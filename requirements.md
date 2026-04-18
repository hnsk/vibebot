I need an IRC bot.

# Core features
- Written in Python
- Packaged as a Python wheel and a Docker image
- Support multiple servers
- Support for bot actions must be modular ie. a module per purpose
- There must be support for ACLs (only users with certain nick/ident/hostname combination can perform tasks)
- You must be able to specify bot nick, realname etc. per network
- There must be a terminal, web and API interfaces (terminal and web must use API)
- Bot settings must be stored in SQLite database
- Any periodic tasks must be persisted in case bot gets restarted

# Modules
- Module must not crash main process
- Modules can share libraries if some tasks are more common
- Modules must be able to react on channel/private messages
- Modules must be able to have scheduled tasks (ie. RSS reader that periodically checks for new articles to send on channel/user)
- It must be possible to load/unload/enable/disable modules while bot is running
- Modules must be retrieved from git (github) repository
- There can be more than one repository
- Bot must have basic functionality without modules or defined repositories for modules

# Interface
- See relevant bot information and server connections
- Be able to send messages as the bot (minimal IRC client)
- See channels and their users
- Do user operatios (such as /op /kick /ban) etc.
- Load, unload, enable, disable, add, remove modules
- Define module repositories

