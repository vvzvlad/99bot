"""Smoke gate for the 99bot image, run on the CI runner against an already-built image.

It runs OUTSIDE the image — `python3 ci/smoke.py` on the runner, driving `docker run`
against $SMOKE_IMAGE — because two checks concern the container itself (behaviour with no
environment, the declared CMD). The runner image ships `python3`, not `python`. Standard
library only.

Nothing here logs in or holds a credential. Check (d) does reach Telegram (`start()` opens
a TCP connection), but its marker is logged before the client is constructed, so the
verdict never depends on the network; the fake credentials guarantee the connection gets
nowhere.

Checks:
* (a) the missing-environment guard fires: `config.get_settings()` prints one line and
  calls `os._exit(1)`; with `restart: always` that line is the only sign of a stack that
  lost TG_API_ID;
* (b) the Kurigram API surface the handlers use still exists — imported symbols, called
  methods, and every `register_handler` run against a real `pyrogram.Client`.
  requirements.txt pins nothing from above, so every rebuild takes the newest Kurigram,
  and a renamed symbol inside a decorator only breaks at registration time;
* (c) the image's declared command and the file it names agree;
* (d) the real CMD, started with fake credentials, logs its first line.

Failures leave through SystemExit, never `assert` (asserts vanish under PYTHONOPTIMIZE=1).
Every check runs before anything is reported, and a check that cannot run reports FAILED.
"""

import json
import os
import subprocess
import sys
import time

# The tag to test. Required rather than defaulted: a default would let a mistyped `env:`
# block in a workflow silently gate some other image that happens to be on the daemon.
IMAGE_ENV = "SMOKE_IMAGE"
# Base name for every container this gate starts. The workflows put the run id in it so
# concurrent runs on the shared runner daemon do not collide. Required, see main().
NAME_ENV = "SMOKE_NAME"

# The short-lived `--rm` containers are named too: `--rm` fires only when a container
# exits, and a subprocess timeout kills the docker client, not the container. A derived
# name lets this script and the workflows' cleanup step remove a hung one (which would
# otherwise also pin the image). Kept in step with the suffix list in both workflows.
GUARD_SUFFIX = "-guard"
PROBE_SUFFIX = "-probe"

# The Dockerfile's WORKDIR and the script its CMD names. Hardcoded rather than read back
# out of the image: these are the contract between this repo and its image, so a Dockerfile
# that quietly moves them has to go red here and be looked at, not be politely followed.
APP_DIR = "/app"
MAIN_REL = "src/main.py"
MAIN_PATH = APP_DIR + "/" + MAIN_REL
SRC_DIR = APP_DIR + "/src"
EXPECTED_CMD = ["python", MAIN_REL]

# Set by the Dockerfile, and NOT decoration. `config.get_settings()` prints its message and
# then calls `os._exit(1)`, which terminates the process without flushing stdout — and
# docker gives a container a pipe, not a tty, so CPython block-buffers it. Without this the
# guard's message is written into a buffer that is thrown away, and check (a) below sees a
# container that exited 1 with an empty log. That is exactly why check (a) matches on the
# message and not only on the exit code: it is what keeps this ENV from being deleted as
# apparently pointless.
UNBUFFERED_ENV = "PYTHONUNBUFFERED=1"

# The one token matched in the guard's output. Deliberately the whole sentence here, unlike
# the sibling repos which match a bare variable name: this string is `config.py`'s only
# output on that path, both variable names are in it, and matching it end to end is what
# distinguishes the guard firing from an ImportError that happens to mention TG_API_ID.
GUARD_MARKER = "TG_API_ID and TG_API_HASH must be set"

# Credentials that are obviously fake and cannot authenticate anywhere: api_id 12345 is the
# placeholder every Pyrogram tutorial uses and the hash is 32 characters of nothing. They
# exist only to get past `get_settings()` and into the code that matters. This is safe
# because `pyrogram.Client(...)` performs NO network I/O — it builds an object and opens
# nothing until `start()` — so checks (b) and (c) never leave the machine at all. Check (d)
# does call `start()`, and these credentials are precisely what guarantees it gets nowhere.
DUMMY_API_ID = "12345"
DUMMY_API_HASH = "0123456789abcdef0123456789abcdef"

# Where the probe and the boot check are told to keep their session file. NOT the image's
# own default (`data`, i.e. /app/data, which in production is the telegram_bots_99bot volume
# holding the real chat_manager_bot.session): pointing it at /tmp keeps every write this
# gate causes inside the container's own writable layer, so nothing it does could ever be
# confused for a session.
SMOKE_SESSION_PATH = "/tmp/smoke-session"

# The first thing `main()` logs, after `setup_logging()` and before anything touches the
# network. Check (d) waits for exactly this: reaching it means every import at the top of
# main.py resolved, the uvloop policy was accepted, and logging is configured — while
# proving nothing about Telegram, which is the point.
BOOT_MARKER = "Starting Telegram Chat Manager Bot"

# Printed by the inner probe on its last line and nowhere else. The probe reports its own
# failures and exits non-zero, so this is belt and braces: it also catches a probe whose
# stdin was truncated, which would otherwise be a python that exited 0 having run half a
# script.
PROBE_MARKER = "99bot smoke probe ok"

# Bounds for every docker call: a hung gate holds a shared runner slot until the step
# timeout kills it, and a killed step skips its cleanup. Generous against the real cost
# (a couple of seconds). The worst-case sum must stay under the smoke step's
# `timeout-minutes` (10) in both workflows:
#   30 (inspect) + 30 (rm) + 90 (guard) + 30 (rm) + 120 (probe) + 30 (rm) + 60 (run -d)
#   + 45 (boot poll) + 30 (rm) = 465 s.
# Three of those `rm`s are pre-run cleanups: a re-run from the UI keeps the run id, hence
# the same $SMOKE_NAME, and must not die on "name already in use".
GUARD_TIMEOUT = 90
PROBE_TIMEOUT = 120
INSPECT_TIMEOUT = 30
REMOVE_TIMEOUT = 30
START_TIMEOUT = 60
LOGS_TIMEOUT = 15

# Check (d)'s poll, bounded in WALL CLOCK rather than in attempts: each attempt shells out
# to `docker logs`, whose own timeout is 15 s, so an attempt-counted bound would multiply
# into minutes the moment the daemon got slow. 30 s of polling is roughly ten times what
# the marker needs — it is logged before any network call, within a second or two of the
# interpreter starting — and the 0.5 s pause keeps a fast container from waiting on us.
BOOT_BUDGET = 30
BOOT_PAUSE = 0.5

# How much of an unexpected output reaches the log. Container logs can run to thousands of
# lines when something loops, and an unbounded dump would bury the verdict.
EXCERPT_CHARS = 4000


# The script fed to `python -u -` inside a container of the image under test, kept as a
# string so the gate stays one file. `-u`: stdout is a pipe, and a probe killed by
# PROBE_TIMEOUT would otherwise lose every line it had printed.
#
# FOUR CONSTANTS ARE DECLARED TWICE, here and inside the probe, and must keep agreeing:
# APP_DIR, SRC_DIR, MAIN_PATH and PROBE_MARKER. Not interpolated, so the probe stays
# runnable by hand as-is; a drift makes the gate go red, never falsely green.
PROBE = r'''
"""Runs INSIDE a container of the image under test, fed to `python -u -` over stdin.

The environment it is given reproduces what `python src/main.py` gets, because that is the
only way its verdict means anything:

* cwd is the image's own WORKDIR — no `-w` is passed, so a Dockerfile that moved it is
  caught below instead of being papered over;
* PYTHONPATH=/app/src, because the real entrypoint is `python src/main.py` and the
  interpreter puts the SCRIPT'S directory on sys.path. Every `from config import ...` and
  `from handlers import ...` in this codebase depends on that, and `python -` (stdin) puts
  the current directory there instead, which is not the same thing;
* TG_API_ID / TG_API_HASH / SESSION_PATH, so `get_settings()` returns instead of calling
  os._exit(1) at import time — `telegram_client.py` calls it at MODULE level.

What it proves that a plain `import` would not: it calls every `register_handler`
function against a live `pyrogram.Client`. Those functions are where the decorators live
(`@client.on_message(filters.command("repic") & filters.group)`,
`@client.on_inline_query(...)`), and a decorator is evaluated at registration time — so
this is the moment a Kurigram release that renamed a filter, an enum or an exception stops
being a theoretical risk and becomes an exception. With requirements.txt pinning nothing,
that release arrives on the next rebuild.

Registration is not the whole surface, though, and the rest is checked with hasattr: the
bodies of those handlers go on to call client and message methods that no import and no
decorator ever touches, so a renamed `reply_text` or `get_chat_history` would sail through
everything above and only surface when a user triggered it in production.

Which modules to check is itself checked, rather than being taken on trust from the list
below: the package is enumerated with pkgutil and compared against it, because a tenth
handler that nobody added to that list is a hole this gate would neither cover nor
mention.
"""

import asyncio
import importlib
import os
import pkgutil
import sys

# These four are the outer script's APP_DIR, SRC_DIR, MAIN_PATH and PROBE_MARKER, written
# out again because this text is handed to another interpreter and cannot import anything
# from the file it lives in. They MUST match the definitions above the PROBE assignment;
# the note there explains why they are copied rather than interpolated, and why a drift
# between the two costs a false red rather than a false green.
APP_DIR = "/app"
SRC_DIR = "/app/src"
MAIN_PATH = "/app/src/main.py"
PROBE_MARKER = "99bot smoke probe ok"

# A value nothing in pyrogram can ever be equal to, used where "this could not be looked up"
# has to stay distinct from "this was looked up and the answer is None". See its use in
# class_method_rows() for the false-green that collapsing the two produces.
MISSING = object()

# Every third-party name the handlers reach for, one row each, so a break names itself
# instead of arriving as a traceback out of a program that got half-way through a message.
# The list is derived from the imports in src/, not from requirements.txt: requirements
# names distributions (Kurigram, Pillow), and what has to keep existing is the SYMBOL.
# An entry of (module, None) means importing the module is itself the check.
SYMBOLS = [
    ("dotenv", "load_dotenv"),
    ("uvloop", "EventLoopPolicy"),
    ("pyrogram", "Client"),
    ("pyrogram", "filters"),
    # The six filters the handlers combine with & and |. They are attributes of a
    # module, so a rename is an AttributeError inside a decorator at registration time —
    # invisible to anything that only imports.
    ("pyrogram.filters", "command"),
    ("pyrogram.filters", "text"),
    ("pyrogram.filters", "group"),
    ("pyrogram.filters", "private"),
    ("pyrogram.filters", "user"),
    ("pyrogram.filters", "service"),
    ("pyrogram.enums", "MessageServiceType"),
    ("pyrogram.enums", "ChatMemberStatus"),
    # The two service-message kinds the watchers dispatch on, reached as a dotted path
    # through the enum class above. The CLASS surviving a release does not mean its members
    # did, and a missing member would not raise at import: `message.service == <gone>`
    # would simply stop matching, and the bot would quietly do nothing forever.
    ("pyrogram.enums", "MessageServiceType.NEW_CHAT_TITLE"),
    ("pyrogram.enums", "MessageServiceType.NEW_CHAT_PHOTO"),
    ("pyrogram.errors", "ChatAdminRequired"),
    ("pyrogram.errors", "ChatNotModified"),
    ("pyrogram.errors", "PhotoInvalidDimensions"),
    ("pyrogram.errors", "PhotoExtInvalid"),
    ("pyrogram.errors", "FloodWait"),
    ("pyrogram.types", "Message"),
    ("pyrogram.types", "InlineQuery"),
    ("pyrogram.types", "InlineQueryResultArticle"),
    ("pyrogram.types", "InputTextMessageContent"),
    ("pyrogram.handlers", "DisconnectHandler"),
    # `from PIL import Image` on a submodule is an import, not an attribute lookup, which
    # is why this is checked as a module first and then for the two constructors
    # repic_watcher calls: `Image.open` on the downloaded sticker and `Image.new` for the
    # white background it flattens transparency onto.
    ("PIL.Image", None),
    ("PIL.Image", "open"),
    ("PIL.Image", "new"),
]

# The Kurigram methods src/ CALLS, as opposed to the symbols it imports. A release that
# renames one of these breaks the bot the first time it is reached — for most of them, the
# first time a user triggers the handler, long after this gate went green and the image
# shipped — and nothing else here would notice: importing a module and running its
# decorators never enters the body of the handler function, which is where nearly every one
# of these lives. The exceptions are the last three, which telegram_client.py calls around
# the connection itself.
# They are checked with hasattr and deliberately never called: nearly all of them perform
# network I/O against Telegram, which this gate does not do. That is weaker than calling
# them, and it is the strongest check available without an account — it catches the rename
# and the removal, which is precisely the failure mode an unpinned requirements.txt keeps
# live.
# Both lists are derived by grepping src/handlers/*.py and src/*.py for calls on the client
# and on the message. A method that stops being called should leave the list; one that
# starts being called should join it.
# NOT in the client list, on purpose: `on_message`, `on_inline_query` and `add_handler`.
# The registration checks below execute all three for real, which proves strictly more than
# hasattr, and duplicating them here would only make a single break report twice.
# `is_connected` is an attribute rather than a method — telegram_client.py branches on it
# before start() and before stop(). It is assigned in `Client.__init__` and does NOT exist
# on the class, which is exactly why this list is checked against a live INSTANCE.
CLIENT_METHODS = [
    "get_me",
    "get_chat_members",
    "get_chat_history",
    "send_message",
    "download_media",
    "set_chat_photo",
    "set_chat_title",
    "start",
    "stop",
    "is_connected",
]

# The message methods, checked on the CLASS: `pyrogram.types.Message` is what the handlers
# annotate their arguments with and what pyrogram hands them at dispatch time, and no
# instance of it can be built here without a real update from Telegram.
# `reply` is NOT here, and its absence is the rule above being applied rather than an
# oversight: every `message.reply(...)` left in src/ is inside a commented-out line in
# repic_watcher.py, so nothing calls it. If those lines are ever restored, it joins the
# list again. Keeping a method here "in case the comments come back" would be the first
# exception to a rule that is only worth having if it has none.
MESSAGE_METHODS = [
    "reply_text",
    "delete",
    "continue_propagation",
]

# The inline-query methods, and the same argument for checking the CLASS: an InlineQuery is
# constructed by pyrogram out of an incoming update, so there is no instance to be had here
# — unlike the client, which this gate builds itself.
# One entry today, from uk_inline_watcher.handle_inline. That is not a reason to leave it
# out: `answer` is the ONLY thing that handler does with the query, so a release that
# renamed it turns the whole inline feature into a handler that runs and replies with
# nothing, and every check above stays green while it happens.
# This is strictly weaker than the registration checks below, and deliberately kept anyway.
# Those checks EXECUTE `@client.on_inline_query(...)`, which proves the decorator still
# exists and still binds — but a decorator only arranges for the body to be called later,
# and `answer` lives in that body, which nothing in this gate ever enters. Registration
# covers getting to the handler; this covers what the handler reaches for once it is there.
INLINE_QUERY_METHODS = [
    "answer",
]

# Every module in src/handlers/, all of them checked, including service_cleaner, whose
# registration is commented out in main.py today: it is imported there unconditionally, so
# it breaks the bot's startup exactly like the other eight, and the day somebody
# uncomments that line the gate should already have been covering it.
HANDLER_MODULES = [
    "rename_watcher",
    "repic_watcher",
    "title_monitor",
    "service_cleaner",
    "short_reply_watcher",
    "history_viewer",
    "pidor_watcher",
    "uk_inline_watcher",
    "lena_watcher",
]


def describe(error):
    return "{}: {}".format(type(error).__name__, error)


def symbol_reason(module_name, attribute):
    """None when the module imports and carries the attribute, else the reason.

    `attribute` may be a dotted path (MessageServiceType.NEW_CHAT_TITLE); it is walked one
    step at a time so the failure names the step that is missing rather than the whole
    path, which is the difference between "the enum is gone" and "a member of it is".
    """
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        # Deliberately broad: a missing distribution raises ImportError, but a wheel built
        # for the wrong platform, a broken C extension or a package whose own import-time
        # code blows up raise something else entirely, and all of them mean the same thing
        # here — this bot will not start in this image.
        return describe(error)
    if attribute is None:
        return None
    current = module
    walked = module_name
    for part in attribute.split("."):
        if not hasattr(current, part):
            return (
                "{} imports cleanly but {} has no attribute {!r} — the dependency is "
                "installed, and a release renamed or removed a name src/ uses. "
                "requirements.txt pins nothing, so this arrives on a rebuild with no "
                "commit behind it".format(module_name, walked, part))
        current = getattr(current, part)
        walked = "{}.{}".format(walked, part)
    return None


def class_method_rows(module_name, class_name, methods, called_on):
    """One row for the class, then one row per method — checked on the CLASS, not on an
    instance.

    Both types this is used for — Message and InlineQuery — are built by pyrogram out of an
    incoming update, so there is no instance to check and no way to make one here. The class
    is what the handlers annotate their arguments with and what pyrogram hands them at
    dispatch time, which makes it the honest place to ask the question.

    The leading row is not decoration: it is what keeps this function from ever returning an
    empty list, so a `methods` list that shrank to nothing makes the report smaller rather
    than making a whole section of it disappear. See the comment on it below.
    """
    rows = []
    # MISSING rather than None, and that is the whole point of it: `getattr` can legitimately
    # hand back None, and using None as the "could not resolve" marker would make those two
    # outcomes indistinguishable. The old code did exactly that, and the failure was silent
    # in the worst direction — a class attribute that existed but was None left the marker
    # unset, every method row below took the `owner is None` branch, and each was appended
    # with a reason of None, i.e. GREEN, having checked nothing at all. A sentinel that no
    # library value can ever equal is what makes "unavailable" unrepresentable by accident,
    # which is what this file's no-false-greens rule needs.
    owner = MISSING
    class_reason = None
    # Two separate `try` blocks, split by WHERE the failure happened rather than by what was
    # raised. One block covering both statements and telling them apart by exception type
    # reads as though it works and does not: an import that fails with anything other than
    # ImportError would land in the class branch and be reported as "the module imported but
    # does not carry X" — a confident, wrong diagnosis that sends whoever is reading the CI
    # log hunting for a renamed class when the real problem is that the dependency does not
    # load at all. symbol_reason() above already documents those imports as a live case.
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        # Broad by the same policy as symbol_reason(), and for the same reason: a missing
        # distribution raises ImportError, but a wheel built for the wrong platform, a
        # broken C extension or a package whose own import-time code blows up raise
        # something else entirely — and every one of them means the same thing here, that
        # nothing about this class can be established inside this image.
        class_reason = (
            "{}. The module itself did not import, so nothing about {} could be "
            "established and none of the method checks below ran".format(
                describe(error), class_name))
    else:
        try:
            resolved = getattr(module, class_name)
        except Exception as error:
            # The lookup, not the import — the module is there. Normally AttributeError;
            # caught broadly anyway because a module-level `__getattr__` (PEP 562) runs
            # arbitrary code on a missed attribute and may raise anything at all. The
            # message below therefore does NOT assert a renamed class: that is only the
            # usual cause, and asserting it for a `__getattr__` that exploded would repeat,
            # one level down, the confident wrong diagnosis this split exists to end.
            class_reason = (
                "{}. The module imported, but looking up {} in it did not produce a "
                "value, so none of the method checks below could run. Usually that means "
                "a release renamed or removed the class".format(
                    describe(error), class_name))
        else:
            if resolved is None:
                class_reason = (
                    "{}.{} exists but is None. Nothing can be checked against it, and "
                    "treating that as a pass would be this gate reporting success for a "
                    "check it never made".format(module_name, class_name))
            else:
                owner = resolved

    # The class gets a row of its own, before the loop and regardless of how long `methods`
    # is. The invariant it protects is this function's return value: without it, a `methods`
    # list that shrank to empty would make this return NO rows at all, and a whole section of
    # the report would vanish silently rather than shrink visibly — the same failure the
    # pkgutil cross-check exists to prevent one level up. That is not hypothetical here: one
    # of the two lists holds a single entry, and the comment beside it argues against
    # dropping that entry precisely because someone might.
    # This does overlap with SYMBOLS, which already checks pyrogram.types.Message and
    # pyrogram.types.InlineQuery in the general sweep of the API surface. The duplication is
    # deliberate and costs exactly one line of output per class: SYMBOLS is the right place
    # for "does this name still exist", and this row is a precondition for the rows it
    # precedes, which has to be visible next to them to be read as one.
    rows.append((
        "{}.{} resolves, so the {} method check(s) below are worth reading".format(
            module_name, class_name, len(methods)),
        class_reason))

    for method in methods:
        target = "{}.{} carries .{} — {}".format(
            module_name, class_name, method, called_on)
        if owner is MISSING:
            # `not attempted:` in the cascading sense used everywhere else in this file —
            # this row genuinely did not run, because the row above it failed. The class row
            # itself never wears this prefix: it WAS attempted, and it is where the actual
            # error is reported.
            rows.append((target, "not attempted: the class did not resolve (see above)"))
            continue
        # `hasattr` swallows only AttributeError, and that is deliberate here rather than an
        # oversight: the broad catch above is justified by PEP 562, which is about MODULE
        # level `__getattr__`. The equivalent on a class would be a metaclass `__getattr__`,
        # and neither of the two types this runs against has one — so anything else raised
        # here is a genuine surprise, and killing the probe with its traceback beats
        # reporting it as a missing method.
        if not hasattr(owner, method):
            rows.append((target, (
                "the class has no such attribute. {} imported and {} exists, so a release "
                "renamed or removed a method that only ever runs inside a handler body — "
                "nothing at import or registration time would notice, and the bot would "
                "fail the first time it tried to use it".format(module_name, class_name))))
            continue
        rows.append((target, None))
    return rows


def handler_count(client):
    """How many handlers pyrogram currently holds, or None when that is not readable."""
    dispatcher = getattr(client, "dispatcher", None)
    groups = getattr(dispatcher, "groups", None)
    if groups is None:
        return None
    try:
        return sum(len(handlers) for handlers in groups.values())
    except Exception:
        return None


async def settle():
    """Give pyrogram's own bookkeeping a chance to run.

    `Client.add_handler` does not append the handler inline: it schedules a coroutine on
    the running loop. Reading the registry in the same tick would therefore always see the
    count from before, and every registration below would look like it did nothing.
    """
    for _ in range(10):
        await asyncio.sleep(0)


async def exercise(client_module, modules):
    """Build the client and register every handler module against it."""
    rows = []
    client = None

    target = ("construct TelegramClient() — builds pyrogram.Client and attaches the "
              "DisconnectHandler; opens no socket, start() does that")
    if client_module is None:
        rows.append((target, "not attempted: telegram_client did not import (see above)"))
    else:
        try:
            client = client_module.TelegramClient().client
            rows.append((target, None))
        except Exception as error:
            rows.append((target, describe(error)))

    target = ("the DisconnectHandler survived registration (client.disconnect_handler is "
              "set) — production restarts on it after three disconnects")
    if client is None:
        rows.append((target, "not attempted: there is no client (see above)"))
    elif getattr(client, "disconnect_handler", None) is None:
        rows.append((target, (
            "it is unset. TelegramClient.__init__ called add_handler(DisconnectHandler), "
            "so either that call stopped taking effect or pyrogram now stores it "
            "elsewhere. Either way the reconnect-limit restart in telegram_client.py is "
            "dead code, and nothing at runtime would say so")))
    else:
        rows.append((target, None))

    # The client methods, on the live instance rather than on pyrogram.Client, because
    # `is_connected` is assigned in __init__ and does not exist on the class at all.
    for method in CLIENT_METHODS:
        target = ("the client carries .{} — src/ reaches for it at runtime, from a handler "
                  "body or from telegram_client.py, where nothing above ever looks".format(
                      method))
        if client is None:
            rows.append((target, "not attempted: there is no client (see above)"))
            continue
        if not hasattr(client, method):
            rows.append((target, (
                "the instance has no such attribute. Every import above still resolved, so "
                "this is a Kurigram release that renamed or removed something src/ calls "
                "outside import and registration time: the bot would start, register all "
                "its handlers, and raise AttributeError the first time it got there")))
            continue
        rows.append((target, None))

    # One row for the registry itself. When it is unreadable the per-handler rows below
    # fall back to "register_handler did not raise", which proves strictly less — so this
    # failure is reported on its own line rather than being folded into every one of them,
    # where it would read as a crowd of unrelated breakages.
    registry_readable = False
    target = ("pyrogram's handler registry is readable (client.dispatcher.groups), so the "
              "registrations below can be verified and not merely attempted")
    if client is None:
        rows.append((target, "not attempted: there is no client (see above)"))
    elif handler_count(client) is None:
        rows.append((target, (
            "client.dispatcher.groups is not a readable mapping in this Kurigram release. "
            "The per-handler checks below still run, but they can only report that "
            "register_handler did not raise. Point handler_count() at wherever the "
            "handlers now live to restore the stronger check")))
    else:
        registry_readable = True
        rows.append((target, None))

    for name in HANDLER_MODULES:
        if registry_readable:
            target = ("handlers.{}.register_handler(client) registers at least one "
                      "handler".format(name))
        else:
            target = ("handlers.{}.register_handler(client) does not raise (the registry "
                      "is unreadable — see the failure above)".format(name))
        module = modules.get(name)
        if module is None:
            rows.append((target, "not attempted: the module did not import (see above)"))
            continue
        if client is None:
            rows.append((target, "not attempted: there is no client (see above)"))
            continue
        before = handler_count(client)
        try:
            # No `group=` argument: main.py passes group=0 explicitly to every handler it
            # registers (its only group=1 call, service_cleaner, is commented out today),
            # but every register_handler defaults to its own value, so calling it bare
            # exercises the same decorators without this gate having to duplicate main.py's
            # grouping — which would then need updating in two places.
            module.register_handler(client)
        except Exception as error:
            rows.append((target, (
                "register_handler raised {} — this is the decorator body failing, i.e. "
                "the Kurigram API the handler is written against has moved".format(
                    describe(error)))))
            continue
        await settle()
        after = handler_count(client)
        if before is None or after is None:
            rows.append((target, None))
            continue
        if after <= before:
            rows.append((target, (
                "it returned without raising, but the registry still holds {} handler(s) "
                "— nothing was registered, so the bot would start, connect, and answer "
                "nothing at all".format(after))))
            continue
        rows.append(("{} (+{})".format(target, after - before), None))

    return rows


def main():
    rows = []

    cwd = os.getcwd()
    rows.append((
        "the container's working directory is {}".format(APP_DIR),
        None if cwd == APP_DIR else (
            "it is {!r}. The Dockerfile's WORKDIR moved, and the image's CMD is the "
            "RELATIVE path `python src/main.py`, which resolves against it".format(cwd))))

    rows.append((
        "{} exists in the image".format(MAIN_PATH),
        None if os.path.isfile(MAIN_PATH) else (
            "there is no such file. The image's CMD names a script the image does not "
            "carry — most likely a COPY that no longer matches the tree — so every "
            "container of it dies on start")))

    rows.append((
        "{} is on sys.path".format(SRC_DIR),
        None if SRC_DIR in sys.path else (
            "it is not; sys.path is {!r}. The probe was started without "
            "PYTHONPATH={}, so the imports below do NOT reproduce what `python "
            "src/main.py` does and their verdict would be meaningless".format(
                sys.path, SRC_DIR))))

    for module_name, attribute in SYMBOLS:
        if attribute is None:
            target = "import {}".format(module_name)
        else:
            target = "from {} import {}".format(module_name, attribute)
        rows.append((target, symbol_reason(module_name, attribute)))

    # The message and inline-query methods, on their classes. `pyrogram.types.Message` and
    # `pyrogram.types.InlineQuery` are themselves among the SYMBOLS above; this walks one
    # step further, into the methods the handler bodies call on the object they were handed.
    rows.extend(class_method_rows(
        "pyrogram.types", "Message", MESSAGE_METHODS,
        "handler bodies call it on the message they are handed"))
    rows.extend(class_method_rows(
        "pyrogram.types", "InlineQuery", INLINE_QUERY_METHODS,
        "uk_inline_watcher calls it on the inline query it is handed"))

    # Exactly what src/main.py does before it touches anything else. Doing it here is not
    # ceremony: it means the registrations below run on a uvloop loop, which is the loop
    # production runs on, and it turns "uvloop imports" into "uvloop's policy is accepted
    # and can actually build a loop".
    reason = None
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception as error:
        reason = describe(error)
    rows.append((
        "asyncio.set_event_loop_policy(uvloop.EventLoopPolicy()), as src/main.py does",
        reason))

    # HANDLER_MODULES is written by hand, and a hand-written list of files stops matching
    # its directory the moment somebody adds one. Nothing below would say so: an unlisted
    # tenth handler is simply never imported, never registered and never mentioned, so the
    # gate keeps reporting the same confident green while covering less. This row is what
    # turns that silent loss of coverage into a failure — the same principle as everywhere
    # else in this file, that a check which cannot be run counts as a check that failed.
    reason = None
    try:
        package = importlib.import_module("handlers")
        present = set(name for _, name, _ in pkgutil.iter_modules(package.__path__))
    except Exception as error:
        reason = (
            "the handlers package could not be enumerated ({}), so whether this list still "
            "covers src/handlers/ is unknown".format(describe(error)))
    else:
        listed = set(HANDLER_MODULES)
        if present != listed:
            reason = (
                "they disagree. In src/handlers/ but not in HANDLER_MODULES: {} — those "
                "handlers are imported by main.py and shipped, and nothing in this gate "
                "looks at them. In HANDLER_MODULES but not in src/handlers/: {} — this "
                "gate is still hunting for a module that was renamed or deleted".format(
                    sorted(present - listed) or "none",
                    sorted(listed - present) or "none"))
    rows.append((
        "HANDLER_MODULES lists exactly the modules in src/handlers/ (expecting {})".format(
            len(HANDLER_MODULES)),
        reason))

    modules = {}
    for name in HANDLER_MODULES:
        dotted = "handlers." + name
        try:
            modules[name] = importlib.import_module(dotted)
            reason = None
        except Exception as error:
            reason = describe(error)
        rows.append(("import {}".format(dotted), reason))

    client_module = None
    try:
        # Imported by name rather than with `import telegram_client` so that a failure is
        # a row like every other one instead of killing the probe before it reports every
        # check above it.
        client_module = importlib.import_module("telegram_client")
        reason = None
    except Exception as error:
        reason = describe(error)
    rows.append(("import telegram_client (calls get_settings() at module level)", reason))

    try:
        rows.extend(asyncio.run(exercise(client_module, modules)))
    except Exception as error:
        # asyncio.run itself failing is its own finding — it means the uvloop policy set
        # above cannot produce a working loop, which is precisely how production starts.
        rows.append((
            "run the registration checks on a uvloop event loop",
            "asyncio.run() raised {} before any handler could be registered".format(
                describe(error))))

    failures = []
    for target, reason in rows:
        if reason is None:
            print("ok   {}".format(target))
        else:
            print("FAIL {} -> {}".format(target, reason))
            failures.append(target)

    if failures:
        print("probe FAILED: {}/{} targets broken".format(len(failures), len(rows)))
        raise SystemExit(1)

    print("{}: {}/{} targets".format(PROBE_MARKER, len(rows), len(rows)))


if __name__ == "__main__":
    main()
'''


def excerpt(text):
    """Bound what reaches the log, and say so when something was cut."""
    if text is None:
        return ""
    if len(text) <= EXCERPT_CHARS:
        return text
    return text[:EXCERPT_CHARS] + "\n[... truncated at {} characters]".format(EXCERPT_CHARS)


def docker(args, timeout, stdin_text=None):
    """Run a docker command.

    Returns (status, output) with stderr folded into stdout, because everything here is
    read by a human out of a CI log where the interleaving is the useful part. A status of
    None means the command produced no exit code at all — it timed out, or docker is not
    there — and `output` then explains which.
    """
    argv = ["docker"] + args
    try:
        completed = subprocess.run(
            argv,
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True)
    except FileNotFoundError:
        return None, (
            "`docker` is not on PATH. This gate drives the daemon from the runner, so it "
            "cannot run anywhere the docker CLI is missing")
    except subprocess.TimeoutExpired as error:
        return None, (
            "`{}` did not finish within {} s. Output so far:\n{}".format(
                " ".join(argv), timeout, excerpt(error.output)))
    return completed.returncode, completed.stdout or ""


def remove_container(name):
    """Best effort. Never the reason a check fails; the workflow cleans up too."""
    docker(["rm", "-f", name], REMOVE_TIMEOUT)


def check_image_config(image):
    """What the image DECLARES: its command, where it runs, and the buffering ENV."""
    rows = []
    status, output = docker(
        ["inspect", "--format", "{{json .Config}}", image], INSPECT_TIMEOUT)
    if status != 0:
        rows.append((
            "docker inspect {}".format(image),
            "exited {} — the tag does not exist on this daemon, so the build step and "
            "this step disagree about what was built. Output:\n{}".format(
                status, excerpt(output))))
        return rows
    try:
        config = json.loads(output)
    except ValueError as error:
        rows.append((
            "parse the image config of {}".format(image),
            "docker inspect returned something that is not JSON ({}): {}".format(
                error, excerpt(output))))
        return rows

    cmd = config.get("Cmd")
    rows.append((
        "the image's CMD is {}".format(EXPECTED_CMD),
        None if cmd == EXPECTED_CMD else (
            "it is {!r}. Every check in this gate that runs the image's own command is "
            "about THAT command, and production runs it too — a silent change here is a "
            "bot that starts something else".format(cmd))))

    # An ENTRYPOINT would prepend itself to the CMD above, so `docker run <image>` would no
    # longer be `python src/main.py` and check (a)'s verdict would be about something else
    # entirely. There is none today, and this is what keeps it that way.
    entrypoint = config.get("Entrypoint")
    rows.append((
        "the image declares no ENTRYPOINT, so its CMD is the whole command line",
        None if not entrypoint else (
            "it declares {!r}, which is prepended to the CMD. Every `docker run` in this "
            "gate now executes something other than what it reports".format(entrypoint))))

    working_dir = config.get("WorkingDir")
    rows.append((
        "the image's WORKDIR is {}".format(APP_DIR),
        None if working_dir == APP_DIR else (
            "it is {!r}, and the CMD is the relative path {}, which resolves against "
            "it".format(working_dir, MAIN_REL))))

    env = config.get("Env") or []
    rows.append((
        "the image sets {}".format(UNBUFFERED_ENV),
        None if UNBUFFERED_ENV in env else (
            "it does not; its Env is {!r}. get_settings() prints its missing-variable "
            "message and then calls os._exit(1), which does NOT flush stdout — and a "
            "container's stdout is a pipe, so with block buffering that message is thrown "
            "away and a misconfigured deployment exits 1 with an empty log".format(env))))

    return rows


def check_missing_env_guard(image, name):
    """Run the image with NO environment at all: it must die, and say why.

    This is the check that cannot live inside the container, and the one that most directly
    describes a real deployment failure — a stack redeployed without TG_API_ID. It is also
    the behavioural half of the PYTHONUNBUFFERED check above: the message only survives
    os._exit(1) because of that ENV.

    The image's own CMD is kept — no `-e`, no override. That is safe to run to completion
    here precisely because the guard makes it terminate on its own; there is no restart
    loop in this image to hang on.

    `--rm` AND a name: the flag covers the container that exits, the name covers the one
    that does not. See GUARD_SUFFIX above for why the second half is not redundant.
    """
    rows = []
    target = "`docker run --rm {}` with no environment at all".format(image)
    # For the same reason check_boot does it: a re-run from the UI reuses the run id, so it
    # reuses this name too, and a previous attempt killed by the step timeout may have left
    # the container behind. A no-op when it did not.
    remove_container(name)
    status, output = docker(["run", "--rm", "--name", name, image], GUARD_TIMEOUT)
    if status is None:
        rows.append((target, output))
        return rows, output
    if status == 0:
        rows.append((target, (
            "it exited 0. The missing-environment guard in config.get_settings() did not "
            "fire, so a stack redeployed without TG_API_ID or TG_API_HASH would come up "
            "looking fine and never connect. Output:\n{}".format(excerpt(output)))))
        return rows, output
    rows.append(("{} exits non-zero (got {})".format(target, status), None))

    # A non-zero exit on its own is NOT enough: an ImportError or a SyntaxError exits
    # non-zero too and would make this check pass for entirely the wrong reason. The
    # guard's own message is the only thing that tells those apart — and, because
    # os._exit(1) never flushes, its presence is also the proof that PYTHONUNBUFFERED is
    # doing its job.
    rows.append((
        "...and its output carries the guard's message, {!r}".format(GUARD_MARKER),
        None if GUARD_MARKER in output else (
            "nothing in the output says that. The image exited {}, but this is not the "
            "guard firing — it is the bot dying of something else before it got there, or "
            "the guard's message being swallowed because PYTHONUNBUFFERED was dropped from "
            "the Dockerfile. Output:\n{}".format(status, excerpt(output)))))
    return rows, output


def check_probe(image, name):
    """The import-and-register probe, run inside a container of the image.

    Everything the probe needs comes through `-e`; nothing is baked into the image, so this
    same command can be run by hand against any local tag. `-w` is deliberately NOT passed:
    the probe asserts the working directory itself, and forcing it here would hide a moved
    WORKDIR instead of catching it.

    Named for the same reason as the guard container above: `--rm` is no help at all
    against the container that hangs, and a hung probe is the likeliest of the three —
    it is the one that imports the whole dependency tree.
    """
    target = ("import the full Kurigram surface and run every register_handler() "
              "function inside the image")
    remove_container(name)
    status, output = docker(
        [
            "run", "--rm", "-i", "--name", name,
            "-e", "PYTHONPATH=" + SRC_DIR,
            "-e", "TG_API_ID=" + DUMMY_API_ID,
            "-e", "TG_API_HASH=" + DUMMY_API_HASH,
            "-e", "SESSION_PATH=" + SMOKE_SESSION_PATH,
            image, "python", "-u", "-",
        ],
        PROBE_TIMEOUT,
        stdin_text=PROBE)
    if status is None:
        return [(target, output)], output
    if status != 0:
        return [(target, (
            "the probe exited {} — see its own FAIL lines in the transcript above, which "
            "name the exact symbol or handler that broke".format(status)))], output
    if PROBE_MARKER not in output:
        # Exit 0 without the marker cannot happen through the probe's own code path, so it
        # means the probe did not run to the end: stdin truncated, or something replaced
        # the interpreter's command line.
        return [(target, (
            "the probe exited 0 but never printed {!r}, so it did not run to its end and "
            "its silence proves nothing".format(PROBE_MARKER)))], output
    return [(target, None)], output


def check_boot(image, name):
    """Start the image's REAL command and wait for its first log line.

    Everything above runs code the gate chose to run. This runs what production runs:
    `python src/main.py` with the environment shape a real deployment has. Reaching
    BOOT_MARKER means main.py's imports resolved, the uvloop policy was accepted and
    logging was configured — under the actual entrypoint.

    Nothing past that line is required, and that is deliberate. The next thing main() does
    is `await tg_client.start()`, which with these fake credentials cannot authenticate;
    with no session file and no tty on stdin, pyrogram's interactive login raises EOFError
    and the container dies. That is the expected outcome and this check does not care,
    because `docker logs` reads an exited container just as well as a running one. Waiting
    for anything that needed Telegram to answer would be a coin toss, and a coin-toss gate
    is worse than none.
    """
    target = ("the image's own command reaches {!r} (bounded at {} s)".format(
        BOOT_MARKER, BOOT_BUDGET))

    # Self-healing retry: re-running a run from the UI keeps the same run id, so $SMOKE_NAME
    # is exactly the name the previous attempt used — and an attempt killed by the step
    # timeout never reaches the workflow's `if: always()` cleanup. Without this the retry
    # dies on `docker run` with "name already in use" and CI stays broken until somebody
    # clears it by hand on the shared runner host. A no-op when nothing was left behind.
    remove_container(name)

    status, output = docker(
        [
            "run", "-d", "--name", name,
            "-e", "TG_API_ID=" + DUMMY_API_ID,
            "-e", "TG_API_HASH=" + DUMMY_API_HASH,
            "-e", "SESSION_PATH=" + SMOKE_SESSION_PATH,
            image,
        ],
        START_TIMEOUT)

    logs = ""
    # The two failure branches below are INSIDE the try, not in front of it. `docker run
    # -d` can create the container and still report failure — and on a timeout it is the
    # docker client that gets killed, while the daemon carries on starting the container it
    # was already asked for. Returning before the `finally` would leave exactly the
    # container this `--name` exists to make removable.
    try:
        if status is None:
            return [(target, output)], ""
        if status != 0:
            return [(target, "`docker run -d` exited {}:\n{}".format(
                status, excerpt(output)))], ""

        deadline = time.monotonic() + BOOT_BUDGET
        while True:
            log_status, logs = docker(["logs", name], LOGS_TIMEOUT)
            if log_status == 0 and BOOT_MARKER in logs:
                return [(target, None)], logs
            if time.monotonic() >= deadline:
                break
            time.sleep(BOOT_PAUSE)
        return [(target, (
            "it never appeared in {} s. The container started, so the image is runnable, "
            "but `python src/main.py` did not get as far as its first log line: an import "
            "at the top of main.py failed, the uvloop policy was rejected, or logging "
            "setup died. Logs:\n{}".format(BOOT_BUDGET, excerpt(logs))))], logs
    finally:
        # The workflow removes this container again under `if: always()`, which covers the
        # case where this whole script is killed by the step timeout and never gets here.
        remove_container(name)


def main():
    image = os.environ.get(IMAGE_ENV)
    if not image:
        print("{} is not set: this gate tests the image it is given and has no default, "
              "because a default would silently gate whichever image happened to be on "
              "the daemon".format(IMAGE_ENV))
        raise SystemExit(1)
    name = os.environ.get(NAME_ENV)
    if not name:
        # No default: the workflow's cleanup step builds its names from the same variable,
        # so a fallback would leak containers silently, and on the shared daemon two
        # concurrent runs would remove each other's containers.
        print("{} is not set: this gate names every container it starts after it, and a "
              "default would both hide containers from the workflow's cleanup step and "
              "make two concurrent runs collide on one name".format(NAME_ENV))
        raise SystemExit(1)

    rows = []
    transcripts = []

    rows.extend(check_image_config(image))

    guard_rows, guard_output = check_missing_env_guard(image, name + GUARD_SUFFIX)
    transcripts.append(("the image run with no environment", guard_output))
    rows.extend(guard_rows)

    probe_rows, probe_output = check_probe(image, name + PROBE_SUFFIX)
    transcripts.append(("the import-and-register probe, inside the image", probe_output))
    rows.extend(probe_rows)

    boot_rows, boot_output = check_boot(image, name)
    transcripts.append(("the image's own command, started with fake credentials",
                        boot_output))
    rows.extend(boot_rows)

    # The transcripts first, the verdicts last: in a CI log the verdicts are what somebody
    # scrolls to the bottom for, and the probe's own per-symbol FAIL lines are what turn
    # "the probe exited 1" into a name.
    for label, text in transcripts:
        print("")
        print("--- {} ---".format(label))
        print(excerpt(text).rstrip() or "(no output)")
    print("")
    print("--- results ---")

    failures = []
    for target, reason in rows:
        if reason is None:
            print("ok   {}".format(target))
        else:
            print("FAIL {} -> {}".format(target, reason))
            failures.append(target)

    if failures:
        print("")
        print("smoke FAILED: {}/{} targets broken:".format(len(failures), len(rows)))
        for target in failures:
            print("  - {}".format(target))
        raise SystemExit(1)

    print("")
    print("smoke ok: {}/{} targets".format(len(rows), len(rows)))


if __name__ == "__main__":
    main()
