# optional-modules — author notes

Rules for anyone (human or agent) touching modules in this directory.

## When adding or updating a module

- Update `optional-modules/README.md` so its entry in the Modules table
  reflects current behavior (name, short description, syntax if user-facing).
- If the module pulls in any external dependency, add or update a
  `requirements.txt` inside the module directory. Pin versions the same way
  the rest of the repo does. Built-in-only modules need no `requirements.txt`.
- Any user-facing `!command` trigger must be a field on the module's
  `Settings` model (see `remindme/__init__.py` — `command: str = Field(...)`)
  and registered dynamically in `on_load` using the current settings value.
  Do not hardcode the prefix; operators must be able to rename it from the
  admin UI.

# Output styling of modules
- IRC Color codes can be used it output. Have to use color combinations that work with BOTH dark and light mode
- IRC Bold can be used in output
- Default output should be styled in a meaningful and easy user readable way relevant to information
- Avoid excessive use of special UTF-8 characters as not all terminals can render them correctly.

## String settings that contain IRC formatting

If a `Settings` string field stores a template with IRC control bytes
(`\x02` bold, `\x03FG,BG` colour, `\x1d` italic, `\x1f` underline,
`\x16` reverse, `\x11` monospace, `\x0f` reset), declare it to the admin
UI so operators get a toolbar + live preview instead of a plain text
input they cannot safely type into:

```python
reply_format: str = Field(
    default="\x02{title}\x02 — {channel}",
    description="Template for the channel reply…",
    json_schema_extra={
        "ui_widget": "irc_format",
        "ui_variables": {
            "title": "Video title.",
            "channel": "Channel name.",
        },
    },
)
```

- `ui_widget: "irc_format"` switches the field to the composite editor
  (toolbar + textarea showing `\xNN` escapes + live preview + optional
  variable list).
- `ui_variables` is an optional `{name: description}` map; click-to-insert
  in the UI. Omit it and variables still work, operators just don't see
  inline docs.
- The module runtime still receives the raw string with real control
  bytes — no code changes needed beyond the schema hint.
