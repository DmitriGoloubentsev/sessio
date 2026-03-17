# Sessio

## Important: Stable Output Format

The output format of `sessio list` must remain stable. Minicode parses it to extract session names.

Current format:
```
sessio <version>
  <name> (pid <pid>)  <cwd>  — <title>
  <name> (pid <pid>)  <cwd>
```

Or when no sessions:
```
sessio <version>
no active sessions
```

Do not change this format without coordinating with Minicode, which depends on parsing these lines.
