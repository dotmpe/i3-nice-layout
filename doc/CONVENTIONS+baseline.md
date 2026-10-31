# Project conventions (User-Tools baseline edition)
- Document project goals, methods, setup or state, and architecture in README.md, succinctly.

- Keep ops/dev-type notes in HACKING.md, or other (doc/* or suitable basename or tree).

- For comments, tasks, open points, remarks and margin notes; the TODO/FIXME/XXX/BUG/NOTE/... tag can be used inline (code comments, strings, or in plain text) to mark things per their category;

  - tasks are tracked in TODO.txt (and moved to DONE.txt on close).
    See github.com/todotxt/todo.txt for the basic syntax rules.

  - TODO.txt entries can use continuation lines for additional free-form body text (this to keep documents/code free from too verbose remarks, and transient or dynamic notes);
    keep these continuation blocks always indented for better parsing (both visually and technically).

    Indicate this additional content on the header line with a classic "line continuation" mark, ie. backslash before the EOL;
    so that the presence is transparent even through line-based consumers, like standard TODO.txt or while e.g. grepping/filtering the header lines only.

- Keep the project base directory tidy; don't use plurals or case sensitive naming, explain new base directories.

  - Source goes into `src/<package>` or `src/<lang>/...`
  - config goes into `config/<package>`
  - documents go into `doc/<package>`

  If there is but only one language, and one package, then those elements can be left out for simpler organizations.

- For development tracks, a source based installation should be a valid deployment option.

  - Tooling support, common leaf utils and such script extra's can go into `tool/<package>` or `tool/<lang>/...`
  - Executable scripts that want to be included at PATH-type name spaces should go into `tool/<lang>/exec` or be grouped at another `tool/*/exec` path; these may be symlinks to scripts with one or more entrypoints.

- Keep it lean, choose and follow conventions.
  For all further project tooling and devops, like provisioning, testing, environment setup and deployment an external project should be appointed the standard; this project just follows versions, and holds bespoke configuration.

----
###### Id: CONVENTIONS+baseline.md +User-Tools v0.1.1
