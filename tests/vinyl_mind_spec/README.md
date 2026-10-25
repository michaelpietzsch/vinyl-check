# Vinyl-Mind specification, version 1 (copy)

An unchanged copy of `spec/v1/` of the [Vinyl-Mind](https://github.com/michaelpietzsch/vinyl-mind) repository (last
changed there in commit `67525f4`: `informative`, feature version 1 stable), the contract between a station and a
hub. The tests check what the station
sends against these schemas and the station's shared scaling against `features.json`; the station itself does not
read them. When the specification changes, copy the directory again and adapt the station.

`examples/invalid/` holds documents a hub must refuse; the station's own validators refuse them too.
