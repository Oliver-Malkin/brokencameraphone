import os
import lib.db as db
import lib.helpers as helpers

from flask import Flask, session, request, flash, send_from_directory
from flask.helpers import url_for
from flask.templating import render_template
from werkzeug.utils import redirect

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp"}

def register_routes(app: Flask):
    @app.get("/game/<joincode>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def game_get(joincode, game, participant):
        if game is None:
            flash("The game you tried to join does not exist.")
            return redirect(url_for("index"))
        
        participant = db.query("select * from participants where user_id = ? and game_id = ?",
                            [session["user_id"], game["id"]], one=True) # type: ignore
        
        state = int(game["state"]) # type: ignore

        if participant is None:
            # can't join - not a participant, and the game has already started
            if state > 0:
                flash("This game is already in progress!")
                return redirect(url_for("index"))
            
            # can join; game not yet started. not yet a participant, so making them one
            else:
                flash("You successfully joined the game.")
                db.query("""
                         insert into participants (user_id, game_id, has_submitted, ordering)
                         values (?, ?, 0, (select count(*) from participants where game_id = ?))
                         """,
                    [session["user_id"], game["id"], game["id"]], # type: ignore
                    commit=True)
                return redirect("/game/" + joincode)
        
        # rejoining a game which the user is already part of
        else:
            template = {
                0: "lobby.html",
                1: "initial-prompt.html",
                2: "photo.html",
                3: "photo-prompt.html",
                4: "game-over.html"
            }[state]

            return render_template(
                template,
                game=game,
                participant=participant,
                previous_submission=get_previous_submission(joincode, participant),
                user_id=session["user_id"],
                is_owner=game['owner_id'] == session["user_id"]) # type: ignore
        
    @app.post("/submit-prompt/<joincode>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def submit_prompt_post(joincode, participant, game):
        if game["state"] not in [1, 3]:
            flash("You can't submit a prompt in this game state!")
            return redirect("/game/" + joincode)
        
        if participant["has_submitted"] > 0:
            flash("You already submitted for this round!")
            return redirect("/game/" + joincode)
        
        prev = get_previous_submission(joincode, participant)
        if prev == None:
            prev = {"root_user": session["user_id"]}
        
        # submit the prompt
        db.query("""
                 insert into submissions (user_id, game_id, round, prompt, root_user)
                 values (?, ?, ?, ?, ?)
                 """,
                 [
                     session["user_id"],
                     game["id"],
                     game["current_round"],
                     request.form["prompt"],
                     prev["root_user"] # type: ignore
                 ],
                 commit=True)
        
        # record that the participant has submitted for this game
        db.query("""
                 update participants
                 set has_submitted = 1
                 where user_id = ? and game_id = ?
                 """,
                 [session["user_id"], game["id"]],
                 commit=True)
        
        if all_submitted(joincode):
            advance_round(joincode, game)

        return redirect("/game/" + joincode)
    
    @app.post("/submit-photo/<joincode>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def submit_photo_post(joincode, participant, game):
        if game["state"] != 2:
            flash("You can't submit a photo in this game state!")
            return redirect("/game/" + joincode)
        
        if participant["has_submitted"] > 0:
            flash("You already submitted for this round!")
            return redirect("/game/" + joincode)
        
        prev = get_previous_submission(joincode, participant)
        if prev == None:
            prev = {"root_user": session["user_id"]}
        
        # submit the photo
        if "photo" not in request.files or request.files["photo"].filename == "":
            flash("No photo uploaded...")
            return redirect("/game/" + joincode)
        
        photo = request.files["photo"]
        allowed, ext = allowed_photo_file(photo.filename)

        if photo and allowed:
            new_filename = f"photo_{joincode}_{participant['user_id']}_{game['current_round']}.{ext}"
            path = os.path.join(app.config["UPLOAD_FOLDER"], new_filename)
            photo.save(path)
        else:
            flash("This file format is not supported. Please use either PNG, JPEG, BMP, or GIF!")
            return redirect("/game/" + joincode)
        
        # submit the photo
        db.query("""
                 insert into submissions (user_id, game_id, round, photo_path, root_user)
                 values (?, ?, ?, ?, ?)
                 """,
                 [
                     session["user_id"],
                     game["id"],
                     game["current_round"],
                     new_filename,
                     prev["root_user"] # type: ignore
                 ],
                 commit=True)
        
        # record that the participant has submitted for this game
        db.query("""
                 update participants
                 set has_submitted = 1
                 where user_id = ? and game_id = ?
                 """,
                 [session["user_id"], game["id"]],
                 commit=True)
        
        if all_submitted(joincode):
            advance_round(joincode, game)

        return redirect("/game/" + joincode)
    
    @app.get("/photo/<path>")
    def get_photo(path):
        return send_from_directory(app.config["UPLOAD_FOLDER"], path)
    
    @app.get("/api/gallery/view/<joincode>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def get_api_gallery_view(joincode, game, participant):
        submissions = db.query(
            """
            select round, photo_path, prompt, display_name, root_user from submissions
            inner join games on games.id = submissions.game_id
            inner join users on users.id = submissions.user_id
            where games.current_showing_user = submissions.root_user and submissions.revealed and games.join_code = ?
            order by round desc
            """, [joincode])
        
        if submissions == None or len(submissions) == 0:
            return {
                "submissions": [],
                "amount": 0,
                "current_showing_user": game["current_showing_user"]
            }
        
        return {
            "submissions": [ {
                "round": s["round"],
                "photo_path": s["photo_path"],
                "prompt": s["prompt"],
                "display_name": s["display_name"]
            } for s in submissions ],
            "current_showing_user": game["current_showing_user"],
            "amount": len(submissions)
        }
    
    @app.get("/api/gallery/set/<joincode>/<user_id>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def get_api_gallery_set(joincode, user_id, game, participant):
        db.query(
            """
            update games 
            set current_showing_user = ?
            where join_code = ?
            """, [user_id, joincode], commit=True)
        
        return {
            "ok": True
        }
    
    @app.get("/api/gallery/advance/<joincode>")
    @helpers.logged_in
    @helpers.with_game("game")
    @helpers.with_participant("participant")
    def get_api_gallery_advance(joincode, game, participant):
        if game['owner_id'] != session["user_id"]:
            return {
                "ok": False
            }
        
        db.query("""
                 update submissions
                 set revealed = 1
                 where id = (
                     select submissions.id from submissions
                     inner join games on games.id = game_id
                     where revealed = 0 and root_user = ? and games.join_code = ?
                     order by round asc
                     limit 1
                 )
                 """, [game["current_showing_user"], joincode], commit=True)
        
        return {
            "ok": True
        }

def allowed_photo_file(filename):
    ext = filename.rsplit(".", 1)[1].lower()
    return ("." in filename and ext in ALLOWED_EXTENSIONS), ext

def all_submitted(joincode):
    not_submitted = db.query(
        """
        select * from participants
        inner join games on participants.game_id = games.id
        where join_code = ? and has_submitted = 0
        """,
        [joincode])
    
    return not_submitted == None or len(not_submitted) == 0

def advance_round(joincode, game):
    db.query(
        """
        update participants
        set has_submitted = 0
        """, commit=True)
    
    print(game["current_round"], game["max_rounds"])
    if game["current_round"] > 2 * game["max_rounds"]:
        # if exceeded max round, game is over
        new_state = 4

        # also, reveal the first prompt of each thread
        db.query(
            """
            update submissions
            set revealed = 1
            where round = 0 and game_id = ?
            """, [game["id"]], commit=True)
    elif game["state"] in [1, 3]:
        # if was doing prompts, change to photos
        new_state = 2
    else:
        # otherwise, change to prompts
        new_state = 3
    
    db.query(
        """
        update games
        set current_round = current_round + 1,
            state = ?
        where join_code = ?
        """, [new_state, joincode], commit=True)

# gets the prompt (or photo prompt) which a player should be
# prompted with in the current round.
# 
# each participant has an "ordering", and we use this to
# decide which participant's prompt each user (the player)
# should be given in each round (the author)
# 
# the ordering of the author is calculated as:
# 
#  author.ordering = (participant.ordering + game.round) % (num. participants)
def get_previous_submission(joincode, participant):
    submission = db.query("""
    select s.user_id, s.game_id, round, photo_path, prompt, root_user from submissions as s
    inner join participants as p on s.user_id = p.user_id and s.game_id = p.game_id
    inner join games as g on s.game_id = g.id
    where g.join_code = ? and s.round = (g.current_round - 1) and p.ordering = (
        select ((1 + ?) % (select count(*) from participants where game_id = games.id)) as ordering
        from games where join_code = ?
    )
    """, [joincode, participant["ordering"], joincode], one=True)

    return submission