# Security policy

## Supported versions

Software Factory is pre-1.0. Security fixes are made on the `main` branch only; there are no
maintained release branches. Please check that a problem is still present on the latest `main`
before you report it.

## Reporting a vulnerability

Please do not report a security problem in a public issue, pull request or discussion.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab, choose
**Report a vulnerability** (<https://github.com/grabskimm/software-factory/security/advisories/new>)
and describe the problem. Only the maintainer can see the report.

Please include:

- what is affected (the commit, and how Factory was run: local mode or the manager, `easy-auth` or
  `entra-jwt`, in-process workers or pods);
- what an attacker needs and what they gain;
- steps to reproduce, or a small proof of concept;
- logs or output with every token, key and private repository name removed.

This project is maintained on a best-effort basis. A fix is prepared privately and published
together with a GitHub security advisory, and you will be credited in it unless you prefer not to
be. Please give the maintainer reasonable time to fix a problem before you disclose it publicly.

## What counts

Factory runs model-written code and holds credentials. A worker is a sandboxed `claude -p` process
that receives `ANTHROPIC_API_KEY` as its only secret and no GitHub credential, and Factory never
merges: it stops at a published pull request plus a `factory/gate` commit status. The trust
boundaries are described in `docs/architecture.md` ("Worker boundaries", "Authentication") and
`docs/operations.md` (the threat models of each feature). In scope:

- an escape from, or a weakening of, the worker boundary (the generated Claude Code settings and
  sandbox, the separate worker identity, `gitsafe`, the dropped capabilities);
- a credential reaching a worker, a log, a run file, a pull request or an issue (the Anthropic API
  key, a GitHub App private key or installation token, the metrics token);
- a bypass of the manager's authentication or roles (`easy-auth`, `entra-jwt`, the `/ui/` page, the
  metrics listener), or a request that makes the manager run anything other than its fixed
  `factory` commands;
- issue, pull request or repository text that can inject arguments, paths or commands into the
  `git`, `gh` or Claude Code processes the supervisor starts.

Out of scope: vulnerabilities in Claude Code, GitHub, Terraform providers or other third-party
software (report them upstream); findings that need an attacker who already holds the Operator role,
has shell access to the host or controls the configuration directory; and running the manager in a
way its documentation forbids, for example reachable by untrusted clients while
`FACTORY_MANAGER_AUTH=easy-auth`, which trusts the `X-MS-CLIENT-PRINCIPAL` header and must only run
on loopback or behind an authenticating proxy.
