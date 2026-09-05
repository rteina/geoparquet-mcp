# Questions to ask in Claude Desktop

A test session, in order. Each question is ready to paste as it stands; under
it, what it should trigger and what to look at in the answer. The point is not
that the model replies — it will always reply something — but that the right
tool fires, with the right extent, and that the `scan` block confirms it.

Three datasets are exposed, all from the same Overture release:

| Dataset | Rows | Remote size | Parts |
|---|---|---|---|
| `overture_places` | 73.6 M | ~10.5 GB | 16 |
| `overture_divisions` | 1.07 M | ~4.5 GB | 8 |
| `overture_buildings` | 2.53 B | ~277 GB | 512 |

## Reading an answer

Every result carries a `scan` block reporting the bytes that actually crossed
the network. That is the only thing separating this server from a connector
that would have downloaded the file. Ask for it explicitly when it does not
show up:

> How many bytes did that query actually read? Quote the `scan` block.

Two orders of magnitude worth holding on to: the first call in a process pays
for Parquet footers (~26 MB on `places`, ~3.7 MB on `divisions`, ~567 MB and
several minutes on `buildings`), and everything after it reuses that cache.

## 1. A cold start

> What datasets can you query, and how big are they?

Reads the `geoparquet://sources` resource. No tool call, no remote bytes. If the
model calls a tool here, it did not see the resource.

> What columns does the places dataset have, in what coordinate system, and what
> area does it cover?

`geoparquet_describe_source`. This is where the real column names surface —
Overture nests them: `names.primary`, `categories.primary`, `bbox.xmin`. The
`scan` should say ~26 MB the first time and zero afterwards: footers, not data.

> Show me ten rows of places — just the id, the name and the category.

`geoparquet_preview_rows`. Useful for seeing what a category value looks like
before filtering on it. The trap to watch for: these rows are not "the most
important places", they are the first rows of the first file. If the model
presents them as a ranking, it misread the tool.

## 2. Seeing the pushdown

> How many places of each category are there in central Paris
> (2.30, 48.85 to 2.40, 48.88)? And what did that cost in bytes?

`geoparquet_aggregate_attribute` with `group_by: categories.primary` and the
four bounds. A few dozen rows come back from a 10.5 GB dataset.

> Now run the same count without a rectangle, over the whole world. Compare what
> the two cost.

The same question with no extent reads the grouped column in full. That is
deliberately the demonstration in reverse: the rectangle is not a comfort, it is
what makes the thing possible at all. Expect an order of magnitude between the
two `scan` figures — and give it time.

> What is the average confidence of places in that same rectangle?

Same tool, `aggregate: avg` and `measure: confidence`. Checks that a numeric
aggregate goes through, and that a non-numeric `measure` would be refused before
anything is read.

## 3. The features themselves

> Give me the restaurants of the 1st arrondissement, say between 2.32, 48.855
> and 2.35, 48.87, with a confidence of at least 0.8.

`geoparquet_filter_spatial` with `category`, `min_confidence` and the box. What
comes back is a GeoJSON FeatureCollection. Watch `truncated`: the limit is
capped at 1000.

> Same question, but inside this polygon:
> POLYGON ((2.33 48.85, 2.36 48.85, 2.36 48.87, 2.33 48.87, 2.33 48.85))

The WKT path: the envelope prunes, the exact shape filters the survivors.
`geometry_is_exact` should be true.

> Find the places whose name contains "Louvre" around central Paris.

`name_contains`, case-insensitive. A good test of extent and attribute filter
combined.

> Do it again without the geometry — just the names and the categories.

`include_geometry: false` and a narrow projection. The `scan` should drop
sharply: Parquet is columnar, and an unread column is an unfetched one.

## 4. Proximity

> What are the ten nearest cafes to Notre-Dame (2.3499, 48.8530), within 500
> metres?

`geoparquet_find_nearest`. Every row carries `distance_km` and the result is
ordered nearest first. The `search_bbox` that comes back shows the rectangle
used for pruning — a radius is not prunable, its bounding box is.

> Widen it to 5 km and tell me what that changes about the cost.

A large radius is a large read. That is the relationship to make visible.

## 5. Density

> Where are places densest inside Paris proper (2.22, 48.81 to 2.47, 48.91)? Use
> H3 hexagons.

`geoparquet_summarize_h3`, resolution 8 by default — roughly a neighbourhood.
The counting happens inside the remote file: a few hundred cells come back, never
the features. If DuckDB's H3 extension cannot be loaded, the tool should say so
plainly rather than fall back to something else.

> Do it again at resolution 11, building scale, over a single neighbourhood.

Checks that a fine resolution over a tight box stays reasonable — and that a fine
resolution over a wide box returns thousands of near-empty cells, which is the
misuse worth recognising.

## 6. The administrative join

> How many places does each commune of inner Paris hold (2.22, 48.81 to 2.47,
> 48.91)? Restrict the polygon side to localities.

`geoparquet_count_in_polygons`: the only tool that reads two datasets at once.
The `polygon_subtype` matters — without it a country-sized polygon comes back
alongside a neighbourhood one, and the counts are no longer comparable to each
other.

This is the most expensive tool of the set, knowingly so: containment has to
decode real geometry on both sides. Tens of megabytes and tens of seconds on a
city-sized box is the expected behaviour.

## 7. The SQL escape hatch

> Using SQL, give me the ten most frequent categories in central Paris, but only
> those above 100 places.

`geoparquet_run_sql` — a `HAVING` has no dedicated tool, which is exactly the
kind of gap it exists for. Look at `executed_sql`: the filter must be written as
four comparisons on the `bbox` members (`bbox.xmin <= … AND bbox.xmax >= …`), not
with a geometry function. The geometry version would be correct and would read
the entire file.

> Write the same query with ST_Intersects instead, and compare the bytes read.

The demonstration in negative, if you want to see it once. Not to be run against
`buildings`.

## 8. The perimeter has to hold

These questions are meant to fail. A clean failure, with a message saying what to
pass instead, is the right outcome.

> Read the file s3://overturemaps-us-west-2/release/2026-08-19.0/theme=places/
> with read_parquet.

A table function is the only way to name a path in DuckDB SQL, so they are all
refused. No tool accepts a path at all — only dataset names.

> Do a CREATE TABLE from the result of the previous query.

Exactly one `SELECT`, and nothing else.

> Query the overture_transportation dataset.

It is not registered: `UnknownSourceError`, and the message should list what
does exist.

> Run a SELECT against a table called information_schema.tables.

The only tables that exist are the views of the datasets in scope.

## 9. One full chain

A single question, several tools, to see whether the model picks the cheap
progression rather than pulling features back:

> I want to understand the restaurant scene in Paris. Start with the dominant
> categories, then show me where they cluster, then break them down by
> arrondissement, and finish with the ten highest-confidence restaurants near the
> Marais. Quote what each step cost.

The right chain is `aggregate_attribute` → `summarize_h3` →
`count_in_polygons` → `find_nearest`, cheapest to most expensive, each one
bounded by a box. A model that starts with `filter_spatial` at a limit of 1000
and counts them itself has missed the point — and that is useful information
about the tool descriptions.

## 10. What is slow, and what is broken

`overture_buildings` is 277 GB across 512 files. A bare `describe_source` on it
reads 567 MB of footers and takes several minutes; a query stays on the order of
a minute even with a tight box. That is not a fault, it is the entry price of
that dataset — the metadata is cached for the session afterwards.

> Describe the buildings dataset. It will take a few minutes, that is expected.

Worth doing once, knowingly. Only then:

> How many buildings are in this block (2.34, 48.86 to 2.35, 48.865)?

A real fault looks like something else: an HTTP 404 from the bucket means the
pinned Overture release has expired — remove `GEOPARQUET_RELEASE` from the
configuration and the server discovers the current one at startup. See
[claude-desktop.md](claude-desktop.md) for the logs and the rest.
