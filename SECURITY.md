# Security

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately through
GitHub instead: on the repository's page open **Security → Report a vulnerability** (GitHub's
private vulnerability reporting). Only the maintainers see the report, and it can become a
security advisory once a fix is out. If that button is missing, open an issue that only asks for
a private way to contact the maintainer, without any details of the problem.

Helpful in a report: which part is affected (the hub on the Pi, the website on the VPS, the web
app, an installer), the commit you looked at, the steps to reproduce it and what an attacker
gains. This is a project maintained in spare time: there is no bug bounty, but every report is
read, and you are credited in the advisory if you like.

## Supported versions

Fixes go to the newest commit of `main`, and from there to the `deploy` branch that automatic
updates follow; there are no release branches. Keep your installation current with
`git pull && sudo pi/install.sh` on the Pi and `git pull && sudo www/deploy.sh` on the VPS (in a git
clone; an uploaded `www/` folder has to be uploaded again), or with
`--auto-update` (README, "Automatic updates").

## What counts

Especially welcome: anything that lets somebody outside the home network control the stove,
lets the website (or someone who runs it) read, change or replay the end-to-end encrypted
messages, takes over an account or a linked stove, gets around the home network checks of
the local dashboard (private addresses, host names, the WebSocket `Origin`, the API tokens), or
gets code onto the `deploy` branch (which automatic updates install as root) without a
maintainer merging it, e.g. through the GitHub Actions workflow.

By design and not a vulnerability: the local dashboard has no login on the home network, so
anyone there can use the stove (README, "The local dashboard"). The Palazzetti Connection Box
itself, its firmware and its own HTTP API are not part of this project; report problems with
them to the manufacturer.
