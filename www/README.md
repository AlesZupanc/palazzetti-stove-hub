# Stove website (VPS part)

Upload this folder to the VPS that serves your site with nginx over HTTPS (or clone the whole
repository there) and run:

```bash
sudo ./deploy.sh --dry-run    # show what would change in nginx
sudo ./deploy.sh              # install / update (safe to re-run)
sudo ./deploy.sh --uninstall  # remove again (--purge also deletes all accounts)
```

What you get: `https://<your-domain>/mystove/` serves the stove web app (`app/`, an installable
app with push notifications) and holds the accounts. The Raspberry Pi at home keeps a WebSocket
open to it (nothing is opened at home); browsers talk to the Pi through it **end-to-end
encrypted** — the website forwards sealed messages, it cannot read or change them. It stores user
names, e-mails, a hash of a key derived from each password (never the password), each account's
encrypted list of stoves, which account may reach which stove, and push endpoints.

On the first run `deploy.sh` asks for the **administrator** (name and e-mail; account requests are
sent there) and how to send **e-mail** — from this server itself or through an e-mail provider —
and prints a one-time link to choose the administrator's password. It keeps your existing nginx routing: it finds the HTTPS `server {}` block of the domain
with `nginx -T`, backs the file up to `/var/backups/stove-cloud/`, adds one
`include /etc/nginx/snippets/stove-cloud.conf;` line (incl. the WebSocket route under
`/mystove/ws/`), runs `nginx -t` (rolling back on failure) and reloads nginx. The website runs as
the systemd service `stove-cloud` (user `stove-cloud`, 127.0.0.1 only, accounts in
`/var/lib/stove-cloud`).

Then, on the Pi, enter `https://<your-domain>/mystove/` as the website address
(`sudo stove-master setup`), open `http://stove.local/` → **Online** at home and use the link to
add the stove to your account.

Options: `--domain NAME`, `--path /mystove`, `--port 8471`, `--server-file FILE`,
`--admin-user NAME`, `--admin-email ADDR`, `--own-mail`, `--skip-mail`, `-y`. Accounts and e-mail
afterwards: `sudo stove-cloud --help`. Logs: `journalctl -u stove-cloud`.

**E-mail from this server** (Debian/Ubuntu):

```bash
sudo ./mail-server.sh             # send-only Postfix on 127.0.0.1 + DKIM signing; lists the DNS records
sudo ./mail-server.sh --check --test-to you@example.com  # after adding the records
```

It needs outgoing port 25 (many VPS providers block it on new servers: then ask them, or use an
e-mail provider with `sudo stove-cloud setup-mail`) and these records: the **reverse DNS** of the
server's IP (at the VPS provider), **SPF**, **DKIM** (`stove._domainkey`) and **DMARC** (at the
domain's DNS). The script prints their exact values and checks them; it changes nothing on a
mail server that was set up for something else.

Full documentation: the README.md in the root of the repository and `docs/protocol.md`
(https://github.com/AlesZupanc/palazzetti-stove-hub).
