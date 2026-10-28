# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private reporting instead:
the repository's *Security* tab, then *Report a vulnerability*.

Include what you found, how to reproduce it, and the version or commit you tested.
This is a hobby project; I'll answer as soon as I can.

## Scope and expectations

- The web interface is built for a trusted home network and must not be exposed directly to the
  internet. Use a reverse proxy with https (see `docs/docker-compose.traefik.yml`) if you need remote access.
- The Tvheadend password and the TMDb key are stored in plain text in `data/config.yaml` (file mode 600),
  because the application needs them to call those services.
