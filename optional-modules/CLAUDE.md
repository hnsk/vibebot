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
