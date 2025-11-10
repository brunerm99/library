# Library

I wanted a nice interface for all the papers, books, articles, etc. I've accumulated, so I ~~created~~ told an LLM to create this personal library (papers not included).

![fmcw search](./static/screenshot-fmcw.png)
![russia search](./static/screenshot-russia.png)

## Prerequisites

- Docker (with compose)

## Setup

- Edit `docker-compose.yml` and update the `volumes` mapping to point to your documents directory, for example:

## Start

- Build and start in the background:
  - `docker compose up --build -d`
- Open http://localhost:8080

## Behavior

- The database and config persist in the `library_data` volume mounted at `/data`.
- Initial scan begins automatically (`LIBINDEX_AUTOSCAN=1`). Use the “Scan” button in the UI to rescan later.
- PDF/EPUB viewers and thumbnails are bundled offline (no Internet required).

## Operations

- View logs: `docker compose logs -f library`
- Rebuild after updates: `docker compose up --build -d`
- Stop: `docker compose down`

## Optional

- To disable auto-scan on startup, set `LIBINDEX_AUTOSCAN=0` in `docker-compose.yml`.

## Autostart (systemd)

- A sample unit is provided at `contrib/systemd/library-index.service`.
  - Edit the `WorkingDirectory` to the absolute path of this repo.
  - Install and enable:
    - `sudo cp contrib/systemd/library-index.service /etc/systemd/system/`
    - `sudo systemctl daemon-reload`
    - `sudo systemctl enable --now library-index.service`
  - This runs `docker compose up -d` on boot and `docker compose down` on stop.
