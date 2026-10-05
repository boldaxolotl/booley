"""Terminal link and path handling used by the Console and Markdown rendering.

Holds the clickable-link resolver (:mod:`.links`) and the raw ``path:line``
pre-processor (:mod:`.path_backtick`), which do not depend on any one
Textual app. Widgets, events, and stylesheets stay with their app.
"""
