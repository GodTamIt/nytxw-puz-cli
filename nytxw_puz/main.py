#!/usr/bin/env python3

from __future__ import annotations

import argparse
import html
import json
import logging
import pendulum
import re
import requests
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from nytxw_puz import decompress, puz

CACHE_DATA = False

# These unicode characters are just used to draw the crossword grid to stdout
BLOCK_LEFT = "\u2590"
BLOCK_MID = "\u2588"
BLOCK_RIGHT = "\u258c"
TITLE_LINE = "\u2501"

# Different possibilities for each cell in the NYT's JSON data structure
NYT_TYPE_BLOCK = 0  # Black cell, no clues or answer
NYT_TYPE_NORMAL = 1  # Normal cell, could be a rebus
NYT_TYPE_CIRCLED = 2  # Cell with a circle around it as for letters part of a theme
NYT_TYPE_GRAY = 3  # A cell filled in as gray
NYT_TYPE_INVISIBLE = 4  # An "invisible" cell, generally something outside the main grid

LATIN1_SUBS = {
    # For converting clues etc. into Latin-1 (ISO-8859-1) format;
    # value None means let the encoder insert a Latin-1 equivalent
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "–": "-",
    "—": "--",
    "…": "...",
    "№": "No.",
    "π": "pi",
    "€": "EUR",
    "•": "*",
    "†": "[dagger]",
    "‡": "[double dagger]",
    "™": "[TM]",
    "‹": "<",
    "›": ">",
    "←": "<--",
    "■": None,
    "☐": None,
    "→": "-->",
    "♣": None,
    "√": None,
    "♠": None,
    "✓": None,
    "♭": None,
    "♂": None,
    "★": "*",
    "θ": "theta",
    "β": "beta",
    "Č": None,
    "𝚫": "Delta",
    "❤︎": None,
    "✔": None,
    "⚓": None,
    "♦": None,
    "♥": None,
    "☹": None,
    "☮": None,
    "☘": None,
    "◯": None,
    "▢": None,
    "∑": None,
    "∃": None,
    "↓": None,
    "⁎": "*",
    "η": "eta",
    "α": "alpha",
    "Ω": "Omega",
    "ō": None,
}

# Some rules to remove HTML like things with text versions for the .puz files
HTML_TO_TEXT_RULES = [
    ("<i>(.*?)</i>", "_\\1_"),  # "<i>Italic</i>" -> "_Italic_"
    ("<em>(.*?)</em>", "_\\1_"),  # "<em>Italic</em>" -> "_Italic_"
    ("<sub>(.*?)</sub>", "\\1"),  # "KNO<sub>3</sub>" -> "KNO3"
    ("<sup>([0-9 ]+)</sup>", "^\\1"),  # "E=MC<sup>2</sup>" -> "E=MC^2"
    (
        "<sup>(.*?)</sup>",
        "\\1",
    ),  # "103<sup>rd</sup>" -> "103rd" (Note, after the numeric 'sup')
    ("<br( /|)>", " / "),  # "A<br>B<br>C" -> "A / B / C"
    (
        "<s>(.*?)</s>",
        "[*cross out* \\1]",
    ),  # "<s>Crossed Out</s>" -> "[*cross out* Crossed out]"
    (
        "<[^>]+>",
        "",
    ),  # Remove other things that look like HTML, but leave bare "<" alone.
    ("&nbsp;", " "),  # Replace HTML's non-breaking spaces into normal spaces
]

logger = logging.getLogger("nyt")
LOG_FORMAT = "%(name)s:%(lineno)d::%(levelname)s: %(message)s"


def set_up_logging(log_level, use_color: bool):
    use_colorlog = False
    if use_color:
        try:
            import colorlog

            use_colorlog = True
        except ImportError:
            pass

    if use_colorlog:
        handler_module = colorlog
        formatter = colorlog.ColoredFormatter(
            f"%(log_color)s{LOG_FORMAT}",
            log_colors={
                "DEBUG": "white",
                "INFO": "blue",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            },
        )
    else:
        handler_module = logging
        formatter = logging.Formatter(LOG_FORMAT)

    logger.setLevel(log_level)

    # Send all logs <= INFO to stdout
    stdout_handler = handler_module.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda record: record.levelno <= logging.INFO)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    # Send all logs >= WARN to stderr
    stderr_handler = handler_module.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    # Now that logger is set up, notify user that colorlog could not be imported.
    if not use_colorlog and use_color:
        logger.warn("Unable to import colorlog dependency for colored logging.")


def try_parse_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except ValueError:
        return None


def try_parse_date(year: str, month: str, day: str) -> Optional[pendulum.DateTime]:
    year = try_parse_int(year)
    month = try_parse_int(month)
    day = try_parse_int(day)

    if all((year, month, day)):
        return pendulum.datetime(year, month, day)

    return None


def latin1ify(s):
    """Make a Unicode string compliant with the Latin-1 (ISO-8859-1) character
    set.

    The Across Lite v1.3 format only supports Latin-1 encoding.
    """

    # Use table to convert the most common Unicode glyphs
    for search, replace in LATIN1_SUBS.items():
        if replace is not None:
            s = s.replace(search, replace)

    # Convert anything remaining using replacements like '\N{WINKING FACE}'
    s = s.encode("ISO-8859-1", "namereplace").decode("ISO-8859-1")

    # Replace HTML like things into plain text
    for pattern, repl in HTML_TO_TEXT_RULES:
        s = re.sub(pattern, repl, s)

    return s


def get_between(haystack: str, start_token: str, end_token: str) -> Optional[str]:
    """Returns the first instance of a string between start and tokens."""

    start_idx = haystack.find(start_token)
    if start_idx == -1:
        return None

    start_idx += len(start_token)

    end_idx = haystack.find(end_token, start_idx)
    if end_idx == -1:
        return None

    return haystack[start_idx:end_idx]


class RateLimitedSession(requests.Session):
    def __init__(self, backoff_secs: float, timeout_secs: float = None):
        super().__init__()

        self._last_req_timestamp = 0.0
        self._backoff_secs = backoff_secs
        self.timeout_secs = timeout_secs

    def _ensure_backoff(self):
        time_elapsed = time.time() - self._last_req_timestamp
        wait_time = self._backoff_secs - time_elapsed

        if wait_time > 0:
            logger.debug(f"Backing off for {wait_time} secs...")
            time.sleep(wait_time)

    def request(
        self,
        method,
        url,
        params=None,
        data=None,
        headers=None,
        cookies=None,
        files=None,
        auth=None,
        timeout=None,
        allow_redirects=True,
        proxies=None,
        hooks=None,
        stream=None,
        verify=None,
        cert=None,
        json=None,
    ):
        self._ensure_backoff()

        # Replace timeout if it's not set
        timeout = timeout if timeout is not None else self.timeout_secs

        result = super().request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            allow_redirects=allow_redirects,
            proxies=proxies,
            hooks=hooks,
            stream=stream,
            verify=verify,
            cert=cert,
            json=json,
        )

        # Record new timestamp right after request completes
        self._last_req_timestamp = time.time()

        return result


@dataclass
class Credentials:
    email: str
    password: str

    @staticmethod
    def try_from_json(path: Path) -> Optional[Credentials]:
        try:
            with path.open("r") as f:
                creds_dict = json.load(f)
        except Exception as e:
            logger.error(f"Unable to load or parse credentials JSON: '{path}'")
            logger.error(e)
            return None

        fail = False
        if "email" not in creds_dict:
            logger.error(f"The 'email' field is missing from the credentials JSON")
            fail = True

        if "password" not in creds_dict:
            logger.error(f"The 'password' field is missing from the credentials JSON")
            fail = True

        if fail:
            logger.debug(f"Credentials: {creds_dict}")
            return None

        return Credentials(creds_dict["email"], creds_dict["password"])


class NytPuzzle:
    @staticmethod
    def try_from_json(json_data: Any) -> Optional[NytPuzzle]:
        if json_data is None:
            return None

        failed = False

        JSON_DATA_FIELDS = ("gamePageData",)
        for field in JSON_DATA_FIELDS:
            if field not in json_data:
                logger.info(f"'{field}' field is missing from puzzle JSON")
                failed = True

        game_data = json_data["gamePageData"]
        GAME_DATA_FIELDS = ("meta", "cells", "dimensions")
        for field in GAME_DATA_FIELDS:
            if field not in game_data:
                logger.info(f"'{field}' field is missing from puzzle JSON")
                failed = True

        dimensions_data = game_data["dimensions"]
        DIMENSIONS_DATA_FIELDS = ("rowCount", "columnCount")
        for field in DIMENSIONS_DATA_FIELDS:
            if field not in dimensions_data:
                logger.info(f"'{field}' field is missing from puzzle JSON")
                failed = True

        if failed:
            return None
        else:
            return NytPuzzle(game_data)

    def __init__(self, game_data: Any):
        self._game_data = game_data

    def title(self) -> str:
        # Determine title based on publication date and title fields
        if "publicationDate" in self._game_data["meta"]:
            title = "NY Times"

            year, month, day = self._game_data["meta"]["publicationDate"].split("-")
            maybe_date = try_parse_date(year, month, day)

            if maybe_date is not None:
                title += f", {maybe_date.format('dddd, MMMM D, YYYY')}"

            if "title" in self._game_data["meta"]:
                title += " - " + latin1ify(self._game_data["meta"]["title"].title())
        elif "title" in self._game_data["meta"]:
            title = latin1ify(self._game_data["meta"]["title"].title())
        else:
            # Fallback on generic title if no fields are available.
            title = "NY Times Crossword"

        return title

    def has_authors(self) -> bool:
        return (
            "constructors" in self._game_data["meta"]
            and self._game_data["meta"]["constructors"]
        )

    def authors(self) -> Iterable[str]:
        return (latin1ify(c) for c in self._game_data["meta"]["constructors"])

    def has_editor(self) -> bool:
        return bool(self._game_data["meta"].get("editor", None))

    def editor(self) -> str:
        return latin1ify(self._game_data["meta"]["editor"])

    def has_copyright(self) -> bool:
        return bool(self._game_data["meta"].get("copyright", None))

    def copyright(self) -> str:
        return (
            f"© {latin1ify(self._game_data['meta']['copyright'])}, The New York Times"
        )

    def height(self) -> int:
        return self._game_data["dimensions"]["rowCount"]

    def width(self) -> int:
        return self._game_data["dimensions"]["columnCount"]

    def has_notes(self) -> bool:
        return bool(self._game_data["meta"].get("notes", None))

    def notes(self) -> Iterable[str]:
        return (
            latin1ify(x["text"])
            for x in self._game_data["meta"]["notes"]
            if "text" in x
        )

    def print(self, file=sys.stdout):
        # Dump out the puzzle, just a helper mostly to debug things
        height = self.height()
        width = self.width()

        for y in range(height):
            row = " "
            extra = ""
            shown = ""
            for x in range(width):
                cell = y * width + x
                cell = self._game_data["cells"][cell]
                if "moreAnswers" in cell:
                    # This is an oddball answer, note all the possibilities
                    row += "- "
                    temp = []
                    if "answer" in cell:
                        temp += [cell["answer"]]
                    temp += cell["moreAnswers"]["valid"]
                    temp = f" ({', '.join(temp)})"
                    if temp != shown:
                        shown = temp
                        extra += temp
                elif "answer" in cell:
                    # Normal answer, but if it's a rebus answer, show the first character
                    # and the rebus answer to the side
                    if len(cell["answer"]) > 1:
                        extra += " " + cell["answer"]
                        row += cell["answer"][0].lower() + " "
                    else:
                        row += cell["answer"] + " "
                else:
                    # Non-clue cell, just mark it
                    row += "# "

            # Turn the "#" into block characters
            for x in range(len(row), 0, -1):
                row = row.replace(
                    " " + "# " * x,
                    BLOCK_LEFT + BLOCK_MID.join([BLOCK_MID] * x) + BLOCK_RIGHT,
                )

            # And output the results
            print(" " + row + extra, file=file)

    def to_puz(self) -> puz.Puzzle:
        """Output the Across Lite [puz.Puzzle] format."""

        out_puz = puz.Puzzle()

        out_puz.title = self.title()

        if self.has_authors() and self.has_editor():
            out_puz.author = f"{', '.join(self.authors())} / {self.editor()}"
        elif self.has_authors():
            out_puz.author = ", ".join(self.authors())
        elif self.has_editor():
            out_puz.author = self.editor()

        if self.has_copyright():
            out_puz.copyright = self.copyright()

        if self.has_notes():
            out_puz.notes = "\n\n".join(self.notes())

        # Pull out the size of the puzzle
        out_puz.height = self.height()
        out_puz.width = self.width()

        cells = self._game_data["cells"]

        # Fill out the main grid
        out_puz.solution = "".join(
            latin1ify(x["answer"][0]) if "answer" in x else "." for x in cells
        )
        out_puz.fill = "".join("-" if "answer" in x else "." for x in cells)

        # And the clues, they're HTML text here, so decode them, Across Lite expects them in
        # crossword order, not the NYT clue order, order them correctly
        seen = set()
        clues = []
        for cell in cells:
            for clue in cell["clues"]:
                if clue not in seen:
                    seen.add(clue)
                    clues.append(
                        latin1ify(html.unescape(self._game_data["clues"][clue]["text"]))
                    )
        out_puz.clues = clues

        # See if any of the answers is multi-character (rebus)
        if max([len(x["answer"]) for x in cells if "answer" in x]) > 1:
            # We have at least one rebus answer, so setup the rebus data fields
            rebus = out_puz.create_empty_rebus()

            # And find all the rebus answers and add them to the data
            for cell in cells:
                if "answer" in cell and len(cell["answer"]) > 1:
                    rebus.add_rebus(latin1ify(cell["answer"]))
                else:
                    rebus.add_rebus(None)

        # See if any grid squares are marked up with circles
        if any(
            x["type"] in (NYT_TYPE_CIRCLED, NYT_TYPE_GRAY) for x in cells if "type" in x
        ):
            markup = out_puz.markup()
            markup.markup = [0] * (out_puz.width * out_puz.height)

            for cell in cells:
                if "type" in cell and cell["type"] in (NYT_TYPE_CIRCLED, NYT_TYPE_GRAY):
                    markup.markup[cell["index"]] = puz.GridMarkup.Circled

        return out_puz


class PuzzleFetcher:
    DEFAULT_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:94.0) Gecko/20100101 Firefox/94.0",
        "DNT": "1",
    }

    PUZZLE_REGEX = re.compile("(pluribus|window.gameData) *= *['\"](?P<data>.*?)['\"]")

    def __init__(
        self,
        backoff_secs: float,
        timeout_secs: float,
        sign_in_limit: int,
        credentials: Credentials,
    ):
        self._req_session = RateLimitedSession(backoff_secs, timeout_secs=timeout_secs)
        self._req_session.headers.update(self.DEFAULT_HEADERS)

        self._sign_in_attempts = 0
        self.sign_in_limit = sign_in_limit

        self._credentials = credentials

    def import_cookies_from_file(self, path: Path) -> bool:
        if not path.is_file():
            logger.warning(f"Invalid cookie file to import: '{path}'")
            return False

        cookie_dict = {}
        try:
            with path.open(mode="r") as f:
                cookie_dict = json.load(f)
        except Exception as e:
            logging.warning(f"Error occurred reading cookie file: '{path}'")
            logging.debug(e)
            return False

        self._req_session.cookies.update(cookie_dict)

        logger.info(f"Successfully imported cookies from '{path}'")
        return True

    def export_cookies_to_file(self, path: Path) -> bool:
        parent_dir = path.parent

        if parent_dir.exists() and not parent_dir.is_dir():
            logger.error(
                f"Cookie export path's parent is not a directory: '{parent_dir}'"
            )
            return False

        parent_dir.mkdir(parents=True, exist_ok=True)

        try:
            with path.open(mode="w") as f:
                json.dump(
                    requests.utils.dict_from_cookiejar(self._req_session.cookies), f
                )
        except Exception as e:
            logger.error(f"Error occurred writing cookie file: '{path}'")
            logging.debug(e)
            return False

        logger.info(f"Successfully exported cookies to '{path}'")
        return True

    def has_login_cookies(self) -> bool:
        LOGIN_COOKIES = [
            "nyt-auth-action",
            "nyt-auth-method",
            "NYT-MPS",
            "NYT-S",
            "SIDNY",
        ]

        for cookie in LOGIN_COOKIES:
            if cookie not in self._req_session.cookies:
                return False

        return True

    def has_reached_sign_in_limit(self) -> bool:
        return self._sign_in_attempts >= self.sign_in_limit

    def sign_in(self, redirect_url: str) -> bool:
        if self.has_reached_sign_in_limit():
            logger.error("Maximum sign in limit reached.")
            return False

        self._sign_in_attempts += 1

        logger.info(f"Attempting login for {self._credentials.email}...")

        token = self._get_sign_in_auth_token(redirect_url)
        if token is None:
            return False

        if not self._post_authorize_email(redirect_url, token):
            return False

        if not self._post_login(redirect_url, token):
            return False

        logger.info("Successfully logged in!")
        return True

    @classmethod
    def _get_enter_email_url(cls, redirect_url: Optional[str] = None) -> str:
        if not redirect_url:
            redirect_url = "https://www.nytimes.com/"

        return f"https://myaccount.nytimes.com/auth/enter-email?application=crosswords&asset=daily-crossword&client_id=games&redirect_uri={requests.utils.quote(redirect_url)}&response_type=cookie"

    def _get_sign_in_auth_token(self, redirect_url: str) -> Optional[str]:
        logger.info("Retrieving sign in auth token...")

        url = self._get_enter_email_url(redirect_url)

        try:
            response = self._req_session.get(url, allow_redirects=True)
        except requests.ConnectTimeout as e:
            logger.info(
                f"Retrieve sign in token request timed out (timeout: {self._req_session.timeout_secs} secs)"
            )
            logger.debug(e)
            return None

        content = response.content.decode("utf-8").strip()

        if not response.ok:
            logger.error(
                f"Failed to load login page 1 of 2 (status code: {response.status_code})"
            )
            logger.debug(f"Body: {content}")
            return None

        token = get_between(content, "authToken&quot;:&quot;", "&quot;,")
        if token is None:
            logger.error("Could not parse login auth token in response")
            logger.debug(f"Body: {content}")
            return None

        token = html.unescape(token)

        logger.debug(f"Auth Token: {token}")

        return token

    def _post_authorize_email(self, redirect_url: str, token: str) -> bool:
        logger.info("Sending authorize email request...")

        url = "https://myaccount.nytimes.com/svc/lire_ui/authorize-email"
        referer = self._get_enter_email_url(redirect_url)

        try:
            response = self._req_session.post(
                url,
                json={
                    "auth_token": token,
                    "email": self._credentials.email,
                    "form_view": "enterEmail",
                },
                headers={
                    "Host": "myaccount.nytimes.com",
                    "Origin": "https://myaccount.nytimes.com/",
                    "Referer": referer,
                },
            )
        except requests.ConnectTimeout as e:
            logger.info(
                f"Authorize email request timed out (timeout: {self._req_session.timeout_secs} secs)"
            )
            logger.debug(e)
            return False

        content = response.content.decode("utf-8").strip()
        if not response.ok:
            logger.error(
                f"Failed to post authorize email (status code: {response.status_code})"
            )
            logger.debug(f"Body: {content}")
            return False

        return True

    def _post_login(self, redirect_url: str, token: str) -> bool:
        logger.info("Sending login request...")

        url = "https://myaccount.nytimes.com/svc/lire_ui/login"
        referer = self._get_enter_email_url(redirect_url)

        try:
            response = self._req_session.post(
                url,
                json={
                    "auth_token": token,
                    "username": self._credentials.email,
                    "password": self._credentials.password,
                    "remember_me": "Y",
                    "form_view": "login",
                },
                headers={
                    "Host": "myaccount.nytimes.com",
                    "Origin": "https://myaccount.nytimes.com/",
                    "Referer": referer,
                },
            )
        except requests.ConnectTimeout as e:
            logger.info(
                f"Login attempt timed out (timeout: {self._req_session.timeout_secs} secs)"
            )
            logger.debug(e)
            return False

        content = response.content.decode("utf-8").strip()
        if not response.ok:
            logger.error(f"Failed to post login (status code: {response.status_code})")

            if content:
                logger.debug(f"Body: {content}")
            return False

        return True

    def fetch_puzzle(self, url: str, max_tries: int) -> Optional[NytPuzzle]:
        tries = 0
        content = None

        while tries < max_tries and content is None:
            tries += 1

            if not self.has_login_cookies():
                if not self.sign_in(url):
                    continue

            content, status_code = self._get_puzzle_page(url)
            if content is None:
                if status_code == None:
                    # Request may have timed out or otherwise failed without a
                    # response
                    continue
                elif status_code == 403:
                    logger.info(
                        "Encountered forbidden 403 code, clearing session cookies and retrying"
                    )
                    self._req_session.cookies.clear()
                    continue
                else:
                    return None

        puzzle_json = self._parse_puzzle_page(content)
        puzzle = NytPuzzle.try_from_json(puzzle_json)
        if puzzle is None:
            logger.error("Failed to parse puzzle JSON")

        return puzzle

    def _get_puzzle_page(self, url: str) -> Tuple[Optional[str], int]:
        logger.info(f"Fetching puzzle page {url}")

        try:
            response = self._req_session.get(url)
        except requests.ConnectTimeout as e:
            logger.info(
                f"Puzzle fetch request timed out (timeout: {self._req_session.timeout_secs} secs)"
            )
            logger.debug(e)
            return None, None

        content = response.content.decode("utf-8").strip()
        if not response.ok:
            logger.error(
                f"Error fetching puzzle page {url} (status code: {response.status_code})"
            )

            if content:
                logger.debug(f"Body: {content}")

            return (None, response.status_code)

        return (content, response.status_code)

    def _parse_puzzle_page(self, content: Optional[str]) -> Optional[Any]:
        if content is None:
            return None

        m = self.PUZZLE_REGEX.search(content)
        if not m:
            logger.error("No puzzle found in puzzle page")

            if content:
                logger.debug(f"Body: {content}")
            return None

        puzzle = m.group("data")
        puzzle = decompress.decode(puzzle)
        puzzle = decompress.decompress(puzzle)

        try:
            puzzle = json.loads(puzzle)
        except Exception as e:
            logger.error("Failed to parse puzzle data as JSON")
            logger.info(f"Puzzle string length: {len(puzzle)}")

            if puzzle:
                logger.debug(e)
                logger.debug(f"Decompressed puzzle: {puzzle}")

            return None

        return puzzle


def read_tasks_from_flags(args: argparse.Namespace) -> List[Tuple[str, Path]]:
    return list(zip(args.urls, args.filenames))


def read_tasks_from_stdin() -> List[Tuple[str, Path]]:
    import csv

    tasks = []
    lines = sys.stdin.readlines()
    reader = csv.reader(lines)
    for row in reader:
        if len(row) != 2:
            logger.error(f"Unexpected stdin CSV entry: `{','.join(row)}`")
            exit(1)

        tasks.append((row[0], Path(row[1])))

    return tasks


def save_puz_to_file(puzzle: puz.Puzzle, path: Path) -> bool:
    parent_dir = path.parent

    if parent_dir.exists() and not parent_dir.is_dir():
        logger.error(f"Puzzle output path's parent is not a directory: '{parent_dir}'")
        return False

    parent_dir.mkdir(parents=True, exist_ok=True)

    puzzle_bytes = puzzle.tobytes()

    try:
        with path.open(mode="wb") as f:
            f.write(puzzle_bytes)
    except Exception as e:
        logger.error(f"Error writing puzzle out to file: '{path}'")
        logging.debug(e)
        return False

    logger.info(f"Successfully wrote puzzle '{puzzle.title}' to '{path.absolute()}'")
    return True


def parse_args() -> argparse.Namespace:
    DEFAULT_BACKOFF = 1500
    DEFAULT_TIMEOUT = 5000
    DEFAULT_MAX_TRIES = 2
    DEFAULT_MAX_SIGN_IN_TRIES = 8

    parser = argparse.ArgumentParser(
        "nytxw_puz",
        description="""CLI tool to convert NY Times crosswords into Across Lite files (.puz).
        
URLs and output filenames can either be specified via the --urls and --filenames flags or via stdin in a CSV `URL,FILENAME` format.""",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        required=True,
        help="the file containing the user credentials for the NY Times website.",
    )
    parser.add_argument(
        "--import-cookies",
        type=Path,
        help="import previously-exported cookies from JSON file.",
    )
    parser.add_argument(
        "--export-cookies", type=Path, help="export cookies to JSON file."
    )
    parser.add_argument(
        "--backoff",
        type=int,
        default=DEFAULT_BACKOFF,
        help=f"the minimum amount of time, in ms, to wait between requests. Default: {DEFAULT_BACKOFF}",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"the maximum amount of time, in ms, to wait for request responses. Default: {DEFAULT_TIMEOUT}",
    )
    parser.add_argument(
        "--max-tries",
        type=int,
        default=DEFAULT_MAX_TRIES,
        help=f"the maximum amount of times to attempt loading a puzzle. Default: {DEFAULT_MAX_TRIES}",
    )
    parser.add_argument(
        "--max-sign-in-tries",
        type=int,
        default=DEFAULT_MAX_SIGN_IN_TRIES,
        help=f"the maximum amount of times to attempt signing in. Default: {DEFAULT_MAX_SIGN_IN_TRIES}",
    )
    parser.add_argument(
        "--print-puzzle",
        "--print-puzzles",
        action="store_true",
        help="print the puzzles to the console.",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="emit logs without colors."
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="verbose output."
    )
    parser.add_argument(
        "--urls", type=str, nargs="*", help="the crossword URL(s) to fetch."
    )
    parser.add_argument(
        "--filenames",
        type=Path,
        nargs="*",
        help="the corresponding location to save each crossword specified in `url`.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.verbose >= 2:
        log_level = logging.DEBUG
    elif args.verbose == 1:
        log_level = logging.INFO
    else:
        log_level = logging.WARN

    set_up_logging(log_level, not args.no_color)

    use_stdin = False
    if args.urls is None and args.filenames is None:
        use_stdin = True
    elif (args.urls is None and args.filenames is not None) or (
        args.filenames is None and args.urls is not None
    ):
        print(
            "If passing data via flags, both --urls and --filenames flags must be set.",
            file=sys.stderr,
        )
        exit(1)
    elif len(args.urls) != len(args.filenames):
        print(
            "The --urls and --filenames flags must have corresponding entries.",
            file=sys.stderr,
        )
        exit(1)

    if use_stdin:
        tasks = read_tasks_from_stdin()
    else:
        tasks = read_tasks_from_flags(args)

    logger.debug(f"{tasks}")

    credentials = Credentials.try_from_json(args.credentials)
    if credentials is None:
        exit(1)

    fetcher = PuzzleFetcher(
        args.backoff / 1000, args.timeout / 1000, args.max_sign_in_tries, credentials
    )

    # Import cookies from file if specified.
    if args.import_cookies:
        fetcher.import_cookies_from_file(args.import_cookies)

    for (url, filename) in tasks:
        if fetcher.has_reached_sign_in_limit():
            logger.error("Maximum sign in limit reached. Aborting all operations.")
            exit(1)

        puzzle = fetcher.fetch_puzzle(url, args.max_tries)

        if puzzle is None:
            continue

        if args.print_puzzle:
            print()
            print(puzzle.title())
            puzzle.print()
            print()

        save_puz_to_file(puzzle.to_puz(), filename)

    # Export cookies to file if specified.
    if args.export_cookies:
        fetcher.export_cookies_to_file(args.export_cookies)


if __name__ == "__main__":
    main()
