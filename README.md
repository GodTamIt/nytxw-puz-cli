# NY Times Crossword to Puz

CLI tool to convert NY Times crosswords into Across Lite files (.puz).

This is a fork of [nytxw_puz](https://github.com/Q726kbXuN/nytxw_puz) with some major differences:

  * No browser installations are required. Authentication is performed by the program itself.
  * Targeted for headless deployments and thus is non-interactive.
  * Supports batch downloading with rate limiting and timeout.

## Usage

```bash
pip install nytxw-puz-cli
nytxw_puz --help
```

### Credentials

The program expects a JSON credentials file to be passed via the `--credentials` flag. The expected format of the JSON is simple and as follows:

```json
{"email": "user@domain.com", "password": "my-password"}
```

### Example Using Flags

A simple example downloading a single crossword:

```bash
nytxw_puz --credentials my-creds.creds --urls https://www.nytimes.com/crosswords/game/daily/2020/12/31 --filenames ~/puzzles/2020/12/31.puz
```

### Example Using stdin

A full example of a run using cookie import/export and input of `URL,FILENAME` entries named `tasks.csv` via stdin:

```bash
nytxw_puz --credentials my-creds.creds --import-cookies mycookies.cookies --export-cookies mycookies.cookies -v < tasks.csv
```

### `nytxw_gen`

`nytxw_gen` is a small tool to help generate date ranges of crossword puzzles in CSV format to pass into `nytxw_puz`. For example:

```bash
nytxw_gen --start 2012-07-18 --end 2014-12-31 --path-format "~/puzzles/<year>/<month>/<day>.puz" > puzzles-to-download.csv 
```


## Development

### Getting Started

This repository uses [poetry](https://python-poetry.org/) to manage dependencies and environments. To get started quickly, run:

```bash
cd nytxw-puz-cli
poetry install
```

### Code Style

For Python, the repository uses the default settings of the [**Black code formatter**](https://black.readthedocs.io/).

Conformance can be enforced by using `black` as follows:

```bash
black <file-name>
```
