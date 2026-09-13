# Documentation thumbnails (source)

These live in veltnex/public/ so vite copies them into the built
static/spa/docs-img/ on `npm run build` (the build empties static/spa,
so images must NOT be placed there directly).

Filename = article id (see src/lib/docs-content.ts), e.g. launch-instance.svg.
Swap any placeholder with a real screenshot by dropping a PNG of the same
base name and updating the article's `image` field.
