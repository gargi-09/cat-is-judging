-- Maritime ground-truth store. This database is the *symbolic* side of the auditor:
-- every fact an agent claims about a vessel or tile must be checkable against these rows.
-- All timestamps are ISO 8601 UTC strings ("YYYY-MM-DDTHH:MM:SSZ") plus an integer epoch
-- column; the string is for humans/LLM tools, the epoch is what range queries index on.

PRAGMA foreign_keys = ON;

-- Static vessel registry: one row per MMSI. Fields that are constant per MMSI in the
-- source feed (verified at ingest time) are normalised out of the position stream.
CREATE TABLE IF NOT EXISTS vessels (
    mmsi         INTEGER PRIMARY KEY,
    imo          INTEGER,            -- NULL when AIS reports 0 (no IMO assigned)
    callsign     TEXT,
    vessel_name  TEXT,
    vessel_type  INTEGER,            -- ITU-R M.1371 ship type; NULL when 0 (not available)
    dim_a        INTEGER,            -- GPS antenna to bow (m)
    dim_b        INTEGER,            -- GPS antenna to stern (m)
    dim_c        INTEGER,            -- GPS antenna to port (m)
    dim_d        INTEGER,            -- GPS antenna to starboard (m)
    length_m     REAL,
    width_m      REAL,
    flag_ctry    TEXT,               -- ISO 3166 alpha-2 as reported; often NULL in source
    n_positions  INTEGER NOT NULL DEFAULT 0
);

-- Dynamic + voyage AIS reports.
CREATE TABLE IF NOT EXISTS ais_positions (
    id             INTEGER PRIMARY KEY,
    mmsi           INTEGER NOT NULL REFERENCES vessels(mmsi),
    postime        TEXT    NOT NULL,  -- ISO 8601 UTC
    postime_epoch  INTEGER NOT NULL,  -- seconds since Unix epoch (UTC)
    lat            REAL    NOT NULL,
    lon            REAL    NOT NULL,
    sog            REAL,              -- knots; NULL for 102.3 "not available"
    cog            REAL,              -- degrees; NULL for >=360 "not available"
    hdg            REAL,              -- degrees; NULL for 511 "not available"
    rot            REAL,              -- deg/min as delivered by the provider; NULL for -128
    nav_status     INTEGER,           -- ITU-R M.1371 navigational status (0-15)
    draught        REAL,
    dest           TEXT,
    eta            TEXT,              -- ISO 8601 UTC
    UNIQUE (mmsi, postime_epoch)
);

CREATE INDEX IF NOT EXISTS idx_ais_mmsi        ON ais_positions (mmsi);
CREATE INDEX IF NOT EXISTS idx_ais_latlon      ON ais_positions (lat, lon);
CREATE INDEX IF NOT EXISTS idx_ais_postime     ON ais_positions (postime_epoch);
-- Covering index for the hot path "where was MMSI x at time t" (track interpolation).
CREATE INDEX IF NOT EXISTS idx_ais_mmsi_time   ON ais_positions (mmsi, postime_epoch);

-- One row per image footprint ("tile"): a Sentinel-2 chip (pixel grid + UTM origin) or,
-- for datasets that ship georeferenced detections, a scene with only a lat/lon box.
-- The georeference lives in the row, so a database is self-contained.
CREATE TABLE IF NOT EXISTS optical_tiles (
    tile_id          TEXT PRIMARY KEY,  -- e.g. "T40RDN_20260303_x3416_y1315"
    product_id       TEXT,              -- e.g. Sentinel-2 SAFE product name
    mgrs_tile        TEXT,
    acquired_at      TEXT NOT NULL,     -- sensing time, ISO 8601 UTC
    acquired_epoch   INTEGER NOT NULL,
    px_x             INTEGER,           -- chip top-left column in the full MGRS raster
    px_y             INTEGER,           -- chip top-left row in the full MGRS raster
    width_px         INTEGER,
    height_px        INTEGER,
    gsd_m            REAL,
    utm_zone         INTEGER,           -- pixel grid georeference (NULL for lat/lon-box scenes)
    utm_south        INTEGER,
    ulx              REAL,
    uly              REAL,
    origin_source    TEXT NOT NULL,     -- metadata | registered | derived | bbox
    lat_min          REAL NOT NULL,
    lat_max          REAL NOT NULL,
    lon_min          REAL NOT NULL,
    lon_max          REAL NOT NULL,
    n_detections     INTEGER NOT NULL,
    json_path        TEXT,              -- annotation source, repo-relative where possible
    png_path         TEXT               -- repo-relative; NULL if no image
);

CREATE INDEX IF NOT EXISTS idx_tiles_bbox ON optical_tiles (lat_min, lat_max, lon_min, lon_max);

-- One row per annotated ship polygon.
CREATE TABLE IF NOT EXISTS optical_detections (
    id                INTEGER PRIMARY KEY,
    tile_id           TEXT NOT NULL REFERENCES optical_tiles(tile_id),
    shape_index       INTEGER NOT NULL,
    label_raw         TEXT,               -- verbatim annotation label, e.g. "538012301.0"
    mmsi              INTEGER,            -- normalised label; NULL for 0/"unknown"/unparseable
    center_lat        REAL NOT NULL,
    center_lon        REAL NOT NULL,
    center_px_x       REAL,               -- chip-local pixel coordinates (NULL without a pixel grid)
    center_px_y       REAL,
    length_m          REAL NOT NULL,      -- hull length; rows without one are dropped, never guessed
    width_m           REAL,
    axis_bearing_deg  REAL,               -- long-axis bearing from true north, [0, 180); NULL if unknown
    shape_type        TEXT,
    polygon_px        TEXT,               -- JSON [[x, y], ...] chip-local
    UNIQUE (tile_id, shape_index)
);

CREATE INDEX IF NOT EXISTS idx_det_mmsi   ON optical_detections (mmsi);
CREATE INDEX IF NOT EXISTS idx_det_latlon ON optical_detections (center_lat, center_lon);

-- Lineage: what produced this database and under which assumptions.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                 INTEGER PRIMARY KEY,
    started_at         TEXT NOT NULL,
    source_file        TEXT NOT NULL,
    source_sha256      TEXT NOT NULL,
    ais_utc_offset     TEXT NOT NULL,
    rows_read          INTEGER NOT NULL,
    rows_loaded        INTEGER NOT NULL,
    rows_dropped_json  TEXT NOT NULL,     -- {"reason": count}
    tiles_loaded       INTEGER NOT NULL,
    detections_loaded  INTEGER NOT NULL,
    dataset_name       TEXT NOT NULL DEFAULT 'default',
    imagery_format     TEXT NOT NULL DEFAULT 'sentinel2_labelme',
    -- How the annotation labels were made. "ais_matched" labels can't measure dark-vessel
    -- recall (it would be circular); only "independent" labels can.
    label_provenance   TEXT NOT NULL DEFAULT 'ais_matched',
    ais_offset_method  TEXT NOT NULL DEFAULT 'manual',  -- manual | auto (label-free sweep)
    offset_sweep_json  TEXT,                            -- the sweep, when auto
    manifest_json      TEXT                             -- the dataset manifest used
);
