#!/usr/bin/env python3

import argparse
import csv
import os
import pendulum
import sys
from pathlib import Path


def eprint(msg: str):
    print(msg, file=sys.stderr)


def parse_date(s: str):
    return pendulum.from_format(s, "YYYY-MM-DD")


def main():
    DEFAULT_YEAR_FORMAT = "YYYY"
    DEFAULT_MONTH_FORMAT = "MM"
    DEFAULT_DAY_FORMAT = "DD"

    parser = argparse.ArgumentParser(
        description="Simple util script to generate URLs and filenames of daily crosswords for nxtxw-puz-cli."
    )
    parser.add_argument(
        "--start",
        type=parse_date,
        required=True,
        help="the starting date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--end",
        type=parse_date,
        required=True,
        help="the ending date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--path-format",
        required=True,
        help="the file path to output each daily crossword to. Use <year>, <month>, and <day> as variable placeholders.",
    )
    parser.add_argument(
        "--year-format",
        default=DEFAULT_YEAR_FORMAT,
        help=f"the Python pendulum format to replace the <year> placeholders with in --path-format. Default: {DEFAULT_YEAR_FORMAT}",
    )
    parser.add_argument(
        "--month-format",
        default=DEFAULT_MONTH_FORMAT,
        help=f"the Python pendulum format to replace the <month> placeholders with in --path-format. Default: {DEFAULT_MONTH_FORMAT}",
    )
    parser.add_argument(
        "--day-format",
        default=DEFAULT_DAY_FORMAT,
        help=f"the Python pendulum format to replace the <day> placeholders with in --path-format. Default: {DEFAULT_DAY_FORMAT}",
    )

    args = parser.parse_args()

    assert args.start < args.end

    path_format: str = args.path_format

    period = pendulum.period(args.start, args.end)

    writer = csv.writer(sys.stdout)
    for date in period.range("days"):
        year = date.format("YYYY")
        month = date.format("MM")
        day = date.format("DD")

        url = f"https://www.nytimes.com/crosswords/game/daily/{year}/{month}/{day}"

        path_year = date.format(args.year_format)
        path_month = date.format(args.month_format)
        path_day = date.format(args.day_format)

        path = os.path.expanduser(
            path_format.replace("<year>", path_year)
            .replace("<month>", path_month)
            .replace("<day>", path_day)
        )
        path = Path(path).absolute()

        writer.writerow((url, path))


if __name__ == "__main__":
    main()
