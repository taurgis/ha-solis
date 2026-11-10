# Changesets

This repository uses [changesets](https://github.com/changesets/changesets) to
version the integration. Add a changeset with `npm run changeset` when a pull
request should appear in the changelog. The workflow on `main` opens a Release
pull request. It does not publish to a package registry.

Do not create tags by hand from an agent run. The release workflow creates the
GitHub release when a `v*` tag is pushed.
