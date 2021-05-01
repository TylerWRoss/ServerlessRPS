from dataclasses import dataclass
import utils
import re
import logging

@dataclass
class CommandResult:
    """ Data class for storing the result of a command (status/success, message to user, optional message to another user) """
    status: int
    message: str
    other_user_number: str = None
    other_user_message: str = None


def setNick(nickname_table, gamestate_table, requestor_number, params):
    """
    Set the requestor_number's nickname in the GameState table
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param requestor_number: E.164 phone number of user
    @param params: Alphanumeric nickname (MUST BE case-insensitively unique, but case will be retained)
    @rtype: CommandResult
    """
    user_game_state = utils.getUserGameState(gamestate_table, requestor_number)
    if user_game_state is not None and 'nickname' in user_game_state.keys():
        return CommandResult(400, "Your nickname is currently set to '{}'. You must 'quit' and re-register, to change it.".format(user_game_state['display_name']))

    try:
        utils.setUserNickname(nickname_table, gamestate_table, requestor_number, params)
    except ValueError as e:
        return CommandResult(400, "Nickname '{}' is invalid. Must be alphanumeric, with no spaces, and may contain underscores.")
    except RuntimeError as e:
        return CommandResult(400, "Nickname {} is taken.".format(params))
    else:
        return CommandResult(200, "Registered nickname {}".format(params))


def throw(nickname_table, gamestate_table, requestor_number, params):
    """
    Play the game! Issue a Rock, Paper, or Scissors throw against some KNOWN 'nick'
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param requestor_number: E.164 phone number of user
    @param params: String of format '<throw> <other_player>', where <throw> is an acceptable throw and <other_player> is a KNOWN player nick.
    @rtype: CommandResult
    """

    if not params:
        return CommandResult(400, "Throw command requires arguments <play> and <other_player_nick>.\n\nReply 'help throw' for details.")

    split = params.split(None, 1)
    if len(split) != 2:
        return CommandResult(400, "Throw command requires arguments <play> and <other_player_nick>.\n\nReply 'help throw' for details.")

    play = split[0].lower()
    play = utils.getRockPaperScissorsPlayFromLeftSubstring(play)
    other_player_nick = split[1]

    if play is None:
        return CommandResult(400, "<play> for throw command must be one of 'rock', 'paper' or 'scissors'.\n\nReply 'help throw' for details.")

    gamestate = utils.getUserGameState(gamestate_table, requestor_number)

    if not 'nickname' in gamestate.keys():
        return CommandResult(400, "You must register a nickname, before you may play.\n\nReply 'help nick' for details.")

    nickname = gamestate['nickname']
    display_name = gamestate['display_name']

    other_player_gamestate = utils.getUserGameStateByNickname(nickname_table, gamestate_table, other_player_nick.lower())

    if other_player_gamestate is None:
        return CommandResult(404, "No player is currently registered with the nickname '{}'.".format(other_player_nick))

    other_player_number = other_player_gamestate['phone_number']
    other_player_nick = other_player_gamestate['nickname']
    other_player_display_name = other_player_gamestate['display_name']

    other_player_lock_uuid = utils.lockUsersGameState(gamestate_table, other_player_number)
    if other_player_lock_uuid is None:
        err = "Failed to lock '{}' (other player for throw)".format(other_player_number)
        logging.error(err)
        raise RuntimeError(err)
    else:
        logging.info("Successfully acquired lock '{}' on '{}' (other player for throw)".format(other_player_lock_uuid, other_player_number))

    try:

        requestor_games = {}
        if 'games' in gamestate.keys():
            requestor_games = gamestate['games']
            requestor_games = utils.removeAbandonedGames(nickname_table, requestor_games)

        other_player_games = {}
        if 'games' in other_player_gamestate.keys():
            other_player_games = other_player_gamestate['games']
            other_player_games = utils.removeAbandonedGames(nickname_table, other_player_games)

        # Check this player for an existing game! No sneaky-changing throws!
        if other_player_nick in requestor_games.keys():
            return CommandResult(403, "You already played {} against {}!".format(requestor_games[other_player_nick], other_player_nick))

        # If the other_player has a pending play/throw against this player (so we can now calculate the winner)
        if nickname in other_player_games.keys():
            other_player_play = other_player_games.pop(nickname) # NOTE: We specifically pop the game out of the dict!

            # Before we determine the winner and message the players, update the gamestate.
            utils.updateUserGameState(gamestate_table, other_player_number, other_player_games)
            utils.updateUserGameState(gamestate_table, requestor_number, requestor_games)

            winner = utils.isPlayerWinner(play, other_player_play)

            if winner is None:
                return CommandResult(200, "You tied with {}".format(other_player_display_name),
                                     other_user_number=other_player_number,
                                     other_user_message="You tied with {}".format(display_name))
            elif winner:
                return CommandResult(200, "You beat {}!".format(other_player_display_name),
                                     other_user_number=other_player_number,
                                     other_user_message="{} beat you".format(display_name))
            else:
                return CommandResult(200, "{} beat you".format(other_player_display_name),
                                     other_user_number=other_player_number,
                                     other_user_message="You beat {}!".format(display_name))

        else:
            requestor_games[other_player_nick] = play

            # Update each player's gamestate (NOTE: other_player's game state will only have changed if stale games were cleared)
            utils.updateUserGameState(gamestate_table, other_player_number, other_player_games)
            utils.updateUserGameState(gamestate_table, requestor_number, requestor_games)

            return CommandResult(200, "Waiting for {}".format(other_player_display_name),
                                 other_user_number=other_player_number,
                                 other_user_message="{} is waiting for you to play against them".format(display_name))


    finally: # In case of exception, we use a finally to attempt to unlock, to ensure we don't leave stale locks!
        unlocked = utils.unlockUsersGameState(gamestate_table, other_player_number, other_player_lock_uuid)
        if not unlocked:
            err = "Failed to unlock '{}' (other player for throw)".format(other_player_number)
            logging.error(err)
            raise RuntimeError(err)
        else:
            logging.info("Successfully cleared lock '{}' on '{}' (other player for throw)".format(other_player_lock_uuid, other_player_number))


def quitGame(nickname_table, gamestate_table, requestor_number):
    """
    'Quit' the ServerlessRPS system: delete user from GameState table
    @param nickname_table: Boto3 DynamoDB Resource Table instance for Nickname Table
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param requestor_number: E.164 phone number of user
    @rtype: CommandResult
    """
    utils.deleteUser(nickname_table, gamestate_table, requestor_number)

    return CommandResult(200, "Your record has been deleted, and your nickname unregistered.")


def helpDoc(command=None):
    """
    Return standard game help to user.
    @rtype: CommandResult
    """
    helpDoc = "Commands:\n\n" + \
        "nick <nickname>: register nickname\n\n" + \
        "throw <play> <other_player_nick>: play against another player\n\n" + \
        "quit: delete player data\n\n" + \
        "help <command>: command-specific help"

    if command:

        command = command.lower()

        if command == 'nick':
            helpDoc = "nick <nickname>\n\n" + \
                "Register nickname for other players to use to play against you.\n\n" + \
                "Nickname must be unique, and alphanumeric (letters, numbers and underscores; no spaces)."

        elif command == 'throw':
            helpDoc = "throw <play> <other_player_nick>\n\n" + \
                "Play against another player (you must know their nickname).\n\n" + \
                "<play> must be one of rock (or r), paper (p), or scissors (s)."

        elif command == 'quit':
            helpDoc = "quit\n\n" + \
                "Issuing this command will clear your player data!\n\n" + \
                "Your nickname will be unregistered (and may be registered by other users).\n\n" + \
                "All in-progress games will be lost."

        else:
            helpDoc = "No specific help doc available for '{}'".format(command)

    return CommandResult(200, helpDoc)


def unknownCommand(gamestate_table, requestor_number, message):
    """
    Handled unparsable messages, unknown commands, etc..
    @param gamestate_table: Boto3 DynamoDB Resource Table instance for GameState Table
    @param requestor_number: E.164 phone number of user
    @param message: Complete, unparsed message which did not map to a known/defined command.
    @rtype: CommandResult
    """
    return CommandResult(400, "Unknown or unparsable command. Reply 'help' or '?' to see valid commands.")
