# Roadmap

The v6.0 roadmap is complete. The release date is not set yet.

## v6.0 Status

| Item | Status |
|------|--------|
| Browser dashboard | Complete |
| API authentication with users and API keys | Complete |
| UPS control through NUT `upscmd` / `upsrw` | Complete |
| Event management API and dashboard deletion | Complete |
| Config hot-reload by `SIGHUP`, `systemctl reload`, and API | Complete |
| Pre-release shutdown-path audit | Complete |
| Pre-release API/dashboard/resource audit | Complete |
| Release date | Unset |

## Notes

NUT auto-discovery was dropped during v6.0 development. It duplicates
`nut-scanner` and does not fit Eneru's config-first model.

Future roadmap items will be rebuilt after v6.0 ships.
