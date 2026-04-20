# optional-modules

First-party modules that ship with the vibebot repository but are not
auto-loaded as built-ins. Add them to your bot by registering this repo in
`config.toml` with `subdir = "optional-modules"`, then enable the desired
module from the admin UI or API.

Example `config.toml` entry:

```toml
[[repos]]
name = "vibebot-optional"
url = "https://github.com/hnsk/vibebot.git"
branch = "main"
subdir = "optional-modules"
```

After the next start the bot clones the repo; enable a module via the web UI
(`/modules`) or API.

## Modules

| Name       | Description                                          |
|------------|------------------------------------------------------|
| `remindme` | Let users schedule personal reminders via chat. Syntax: `!remindme <when> <message>` (e.g. `5m`, `1h30m`, `1 day`). |
