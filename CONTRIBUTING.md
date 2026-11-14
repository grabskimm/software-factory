# Contributing

Thank you for helping. Factory is a small, standard-library-only Python package: most changes are a
code change plus a test.

## Set up

You need Python 3.11 or newer and `git`. Nothing else is needed to run the tests: no API key, no
GitHub token, and neither `gh` nor `claude` (the tests use fakes). Optional tools make a few more tests
run instead of being skipped: `node`, `jq`, `shellcheck`, `terraform` and, for the token broker tests,
the `cryptography` package (`tools/requirements-token-broker.txt`).

```sh
git clone https://github.com/grabskimm/software-factory
cd software-factory
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Instead of installing you can prefix the commands below with `PYTHONPATH=src`.

## Run the tests

```sh
python3 -m unittest discover -s tests               # everything: about 13 minutes
python3 -m unittest tests.test_config -v            # one module
python3 -m unittest discover -s infra/test          # the optional Terraform reference: offline, a few seconds
```

Run them as a normal user, not as root. Root can read what those tests make unreadable, so as root
the permission-based purge tests (`tests/test_purge.py`, class `WorkerOwnedFilesTests`) are skipped.

## Rules of the code

- `src/factory` uses the standard library only and must stay that way. The one exception,
  `tools/token_broker.py`, has its own environment and requirements file.
- Documents are pinned by tests: some tests read the wording of `README.md` and of the files in
  `docs/`. When you change a document, change the test that pins it in the same pull request.
- The skill `.claude/skills/factory/SKILL.md` tells a person or an agent how to drive the CLI;
  `tests/test_skill.py` checks it against the real parser and values. Add a subcommand or a status
  value, and the skill has to learn it too.
- Keep a change small and focused, and say what it does and why. Add or update a test for every
  behaviour change.
- Do not commit run directories, transcripts, tokens, keys, or real repository, tenant or
  subscription identifiers.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to `main`, with no secrets: a
byte-compile and JSON check, the unit tests on Python 3.11, 3.12 and 3.13, the token broker tests in
their own virtual environment, and the offline checks of `infra/`. The job named `CI result` runs
last and is green only when every other job succeeded; it is the one check to require in a branch
ruleset (`docs/handoff.md`). A new job has to be added to its `needs:` list.

## License

Factory is licensed under the Apache License 2.0 (`LICENSE`). By submitting a contribution you agree
that it is licensed under the same terms (section 5 of the license: inbound is outbound). There is no
CLA and no sign-off requirement.

## Security

Please do not open a public issue for a vulnerability; see `SECURITY.md`.
