"""allseer - local research/trend monitor."""
import sys

# Titles from GitHub/Reddit are full of emoji and the Windows console is cp1252, so a
# plain print() of a log line can kill a whole research run. Degrade the character
# instead of the run. Every entry point imports this package first.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # not a TextIOWrapper (redirected, embedded)
        pass
