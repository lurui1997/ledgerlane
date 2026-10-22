# Friction log — Amazon Developer Hackathon (Ledgerlane)


### Entry 1

- Task: confirm how an Alexa+ MCP server must authenticate.
- Steps: read the hackathon rules for the simulation path, then the Alexa+ MCP quickstart authentication checklist.
- Expected: one story for a hackathon MCP server.
- Actual: the rules allow a web simulation with any agent stack, and also a self-hosted MCP server on spec 2025-11-25. The quickstart, aimed at add-on certification, requires a bare 401, protected-resource metadata, and PKCE S256, and says `WWW-Authenticate` is not supported. The Python SDK's auth middleware emits that header.
- Severity: medium.
- Workaround: custom 401 gate with no authenticate header, plus demo OAuth metadata, and the web simulator for the interaction.
- Suggestion: add a "hackathon simulation vs certification" box at the top of the quickstart, and show a 401 example with the header explicitly absent.

### Entry 2

- Task: decide whether the Open Source mini challenge can be the same repository as the Alexa+ entry.
- Steps: read the mini-challenge wording "new, additional open-source project ... alongside a primary track submission."
- Expected: a new Apache-licensed repo would qualify on its own.
- Actual: "additional" can be read as a second artifact. The form also asks for a contribution URL, a project URL, and a GitHub username.
- Severity: low.
- Workaround: treat Open Source as optional until the entrant has a public URL, and describe this repo as the primary entry.
- Suggestion: say whether the primary repo counts when the primary project is itself the new open-source work.

