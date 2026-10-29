# EUC Planet Maps

Offline data for [EUC Planet](https://github.com/eried/eucplanet), mirrored
so the app does not lean on one volunteer server.

## Routing tiles

`routing/index.json` lists the BRouter routing tiles (`.rd5`, one per 5x5
degree square, named by its south-west corner) and `lookups.dat`, each with
its size, upstream date and download link. The files themselves are assets
of the `routing-*` releases.

A GitHub Action (`.github/workflows/sync-routing.yml`) runs on the 2nd of
every month. It uploads only the tiles that changed on brouter.de, commits
the new index, and deletes releases that neither the new nor the previous
index uses.

The app reads the index from:

```
https://raw.githubusercontent.com/eried/eucplanet-maps/main/routing/index.json
```

## Credits and license

- Map data (c) [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, available under the
  [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
  The tiles here are a derived database under the same license.
- Tiles built by [BRouter](https://brouter.de) (MIT), from
  `https://brouter.de/brouter/segments4/`.
- The scripts in this repository are MIT licensed.
