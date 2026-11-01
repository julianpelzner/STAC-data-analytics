"""Generate the synthetic business dataset (customers, AOIs, orders) as CSVs.

Models *demand* only: who orders imagery, where, and when. Order outcomes
(fulfilled? when? by which scene?) are derived later in dbt by joining to the
real STAC data — this generator never decides them. See docs/data_model.md.

Usage:
    python -m ingestion.generate_synthetic --seed 42
    python -m ingestion.generate_synthetic --seed 7 --start 2025-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from faker import Faker
from shapely import affinity
from shapely.geometry import Polygon

from ingestion import config
from ingestion.config import AOI
from ingestion.pull_stac_metadata import default_window

logger = logging.getLogger("ingestion.generate_synthetic")

# Great-circle km per degree of latitude is ~constant; km per degree of
# longitude shrinks with latitude. Good enough to size ~1-100 km AOIs inside
# a ~1 degree bbox without pulling in a projection library.
KM_PER_DEG_LAT = 110.574


def km_per_deg_lon(lat_deg: float) -> float:
    return 111.320 * math.cos(math.radians(lat_deg))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _unit_blob(rng: np.random.Generator, n_vertices: int, jitter: float) -> Polygon:
    """Star-shaped polygon around the origin: simple (never self-intersecting)
    because every vertex is sorted by angle. `jitter` sets radial raggedness,
    which is what pushes compactness down."""
    n = max(3, n_vertices)
    # One vertex per angular sector, offset inside the sector so angles stay
    # strictly increasing and distinct -> the polygon is always simple.
    sector = 2 * math.pi / n
    base = (np.arange(n) + rng.uniform(0.15, 0.85, n)) * sector
    radii = 1.0 + jitter * rng.uniform(-1, 1, n)
    radii = np.clip(radii, 0.1, None)
    coords = [(r * math.cos(t), r * math.sin(t)) for t, r in zip(base, radii)]
    return Polygon(coords)


def _unit_corridor(rng: np.random.Generator, n_vertices: int, jitter: float, aspect: float) -> Polygon:
    """Long thin strip of length:width = aspect. Vertices are split between the
    two long edges, so a corridor gets the very low compactness pipelines and
    transmission lines have in reality."""
    per_edge = max(2, n_vertices // 2)
    half_w = 0.5
    half_l = 0.5 * aspect
    xs = np.linspace(-half_l, half_l, per_edge)
    top = [(x, half_w * (1 + jitter * rng.uniform(-1, 1))) for x in xs]
    bottom = [(x, -half_w * (1 + jitter * rng.uniform(-1, 1))) for x in reversed(xs)]
    return Polygon(top + bottom)


def _unit_rectangle(rng: np.random.Generator, n_vertices: int, jitter: float) -> Polygon:
    """Near-rectangle for large, simple admin blocks: start from a box with a
    mild aspect ratio, optionally push extra vertices onto the edges as
    midpoints, keep jitter low so compactness stays high."""
    aspect = rng.uniform(1.0, 1.6)
    half_w, half_l = 0.5, 0.5 * aspect
    corners = [(-half_l, -half_w), (half_l, -half_w), (half_l, half_w), (-half_l, half_w)]
    # Densify edges until we reach the target vertex count (capped; blocks are simple).
    coords = list(corners)
    while len(coords) < min(n_vertices, 8):
        i = rng.integers(0, len(coords))
        a, b = coords[i], coords[(i + 1) % len(coords)]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        coords.insert(i + 1, mid)
    jittered = [(x + jitter * rng.uniform(-1, 1) * 0.1, y + jitter * rng.uniform(-1, 1) * 0.1) for x, y in coords]
    return Polygon(jittered)


def build_aoi_polygon(
    rng: np.random.Generator,
    aoi_shape: str,
    shape_params: dict,
    target_area_km2: float,
    n_vertices: int,
    bbox: tuple[float, float, float, float],
) -> tuple[Polygon, float]:
    """Return a lon/lat polygon of (close to) `target_area_km2` that fits fully
    inside `bbox`, and its measured planar area in km^2.

    The shape is drawn in a local km plane, scaled to the target area, randomly
    rotated, shrunk if it can't fit the bbox, then projected to lon/lat.
    """
    jitter = shape_params.get("radius_jitter", 0.3)
    if aoi_shape == "corridor":
        ar = shape_params.get("aspect_ratio", {"min": 8, "max": 25})
        aspect = rng.uniform(ar["min"], ar["max"])
        unit = _unit_corridor(rng, n_vertices, jitter, aspect)
    elif aoi_shape == "admin_block":
        unit = _unit_rectangle(rng, n_vertices, jitter)
    else:
        unit = _unit_blob(rng, n_vertices, jitter)

    # Scale the unit shape so its area matches the target (in km^2).
    scale = math.sqrt(target_area_km2 / unit.area)
    poly_km = affinity.scale(unit, xfact=scale, yfact=scale, origin=(0, 0))
    poly_km = affinity.rotate(poly_km, rng.uniform(0, 360), origin=(0, 0))

    min_lon, min_lat, max_lon, max_lat = bbox
    center_lat = (min_lat + max_lat) / 2
    kpl_lon = km_per_deg_lon(center_lat)

    # Shrink to fit the bbox if the AOI is wider/taller than the region allows.
    xmin, ymin, xmax, ymax = poly_km.bounds
    width_deg = (xmax - xmin) / kpl_lon
    height_deg = (ymax - ymin) / KM_PER_DEG_LAT
    fit = min(1.0, 0.95 * (max_lon - min_lon) / width_deg, 0.95 * (max_lat - min_lat) / height_deg)
    if fit < 1.0:
        poly_km = affinity.scale(poly_km, xfact=fit, yfact=fit, origin=(0, 0))
        xmin, ymin, xmax, ymax = poly_km.bounds
        width_deg = (xmax - xmin) / kpl_lon
        height_deg = (ymax - ymin) / KM_PER_DEG_LAT

    measured_area = poly_km.area  # km^2, planar (fine at this scale)

    # Place by the shape's actual extent, not its origin: pick the lon/lat
    # offset so [xmin, xmax] lands inside [min_lon, max_lon] (same for lat).
    # `fit` guarantees these intervals are non-empty.
    xmin_deg, xmax_deg = xmin / kpl_lon, xmax / kpl_lon
    ymin_deg, ymax_deg = ymin / KM_PER_DEG_LAT, ymax / KM_PER_DEG_LAT
    cx = rng.uniform(min_lon - xmin_deg, max_lon - xmax_deg)
    cy = rng.uniform(min_lat - ymin_deg, max_lat - ymax_deg)

    coords = [(cx + x / kpl_lon, cy + y / KM_PER_DEG_LAT) for x, y in poly_km.exterior.coords]
    return Polygon(coords), measured_area


def compactness(poly_km: Polygon) -> float:
    """Polsby-Popper: 1 for a circle, toward 0 for thin or jagged shapes.
    Measured in the km plane so it is independent of latitude distortion."""
    return 4 * math.pi * poly_km.area / (poly_km.length ** 2)


# --------------------------------------------------------------------------- #
# Record construction
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    name: str
    cfg: dict


def allocate_counts(shares: dict[str, float], total: int) -> dict[str, int]:
    """Split `total` across segments by share, largest-remainder so it sums exactly."""
    raw = {k: v * total for k, v in shares.items()}
    floored = {k: int(math.floor(v)) for k, v in raw.items()}
    remainder = total - sum(floored.values())
    order = sorted(raw, key=lambda k: raw[k] - floored[k], reverse=True)
    for k in order[:remainder]:
        floored[k] += 1
    return floored


def sample_log_uniform(rng: np.random.Generator, lo: float, hi: float) -> float:
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def choose_region(rng: np.random.Generator, weights: dict[str, int], bboxes: dict[str, AOI]) -> str:
    names = [n for n in weights if n in bboxes]
    w = np.array([weights[n] for n in names], dtype=float)
    return str(rng.choice(names, p=w / w.sum()))


def choose_shape(rng: np.random.Generator, seg_cfg: dict) -> str:
    mix = seg_cfg.get("shape_mix")
    if not mix:
        return seg_cfg["shape_type"]
    types = list(mix)
    p = np.array([mix[t] for t in types], dtype=float)
    return str(rng.choice(types, p=p / p.sum()))


def choose_tier(rng: np.random.Generator, tier_mix: dict[str, float]) -> str:
    tiers = list(tier_mix)
    p = np.array([tier_mix[t] for t in tiers], dtype=float)
    return str(rng.choice(tiers, p=p / p.sum()))


def price_order(area_km2: float, tier: str, priority: str, segment: str, pricing: dict) -> float:
    gross = (
        area_km2
        * pricing["base_rate_per_km2"]
        * pricing["tier_multiplier"][tier]
        * pricing["priority_multiplier"][priority]
        * (1 - pricing["segment_discount"][segment])
    )
    return round(max(pricing["min_order_fee"], gross), 2)


def processing_hours(rng: np.random.Generator, area_km2: float, compact: float, vertices: int, priority: str, pt: dict) -> float:
    hours = (
        pt["base_hours"][priority]
        * (1 + pt["alpha"] * math.log1p(area_km2))
        * (1 + pt["beta"] * (1 - compact))
        * (1 + pt["gamma"] * math.log1p(vertices))
        * rng.lognormal(0.0, pt["sigma"])
    )
    return round(hours, 2)


def _random_time(rng: np.random.Generator) -> time:
    secs = int(rng.integers(0, 86400))
    return time(secs // 3600, (secs % 3600) // 60, secs % 60)


def generate(cfg: dict, aois_cfg: list[AOI], seed: int, start: date, end: date) -> dict[str, pd.DataFrame]:
    """Build the three tables. Pure given (cfg, aois, seed, window): no I/O."""
    rng = np.random.default_rng(seed)
    faker = Faker()
    faker.seed_instance(seed)

    bboxes = {a.name: a for a in aois_cfg}
    pricing, pt = cfg["pricing"], cfg["processing_time"]
    shapes, segments = cfg["shapes"], cfg["segments"]
    ordering = cfg["ordering"]
    total_days = (end - start).days

    shares = {name: seg["customer_share"] for name, seg in segments.items()}
    per_segment = allocate_counts(shares, cfg["volumes"]["n_customers"])

    customers: list[dict] = []
    aois: list[dict] = []
    orders: list[dict] = []

    # Build customers -> AOIs -> orders in one deterministic pass; dates that
    # depend on child records (created_at, signup_date) are backfilled after.
    for segment in segments:  # fixed order -> reproducible ids
        seg_cfg = segments[segment]
        for _ in range(per_segment[segment]):
            cust_id = f"CUST-{len(customers) + 1:05d}"
            customers.append(
                {
                    "customer_id": cust_id,
                    "company_name": faker.company(),
                    "segment": segment,
                    "contract_tier": choose_tier(rng, seg_cfg["tier_mix"]),
                    "country": faker.country_code(representation="alpha-2"),
                    "signup_date": None,  # backfilled
                }
            )

            n_aois = max(1, int(rng.poisson(seg_cfg["aois_per_customer"])))
            for _ in range(n_aois):
                aoi_id = f"AOI-{len(aois) + 1:05d}"
                region = choose_region(rng, seg_cfg["regions"], bboxes)
                shape = choose_shape(rng, seg_cfg)
                target_area = sample_log_uniform(rng, seg_cfg["area_km2"]["min"], seg_cfg["area_km2"]["max"])
                target_vertices = int(rng.integers(seg_cfg["vertex_count"]["min"], seg_cfg["vertex_count"]["max"] + 1))

                poly, area_km2 = build_aoi_polygon(
                    rng, shape, shapes[shape], target_area, target_vertices, bboxes[region].bbox
                )
                # Compactness is measured on the km-plane twin of the polygon, so
                # longitude distortion near the poles doesn't skew it.
                center_lat = (bboxes[region].bbox[1] + bboxes[region].bbox[3]) / 2
                poly_km = affinity.scale(
                    poly, xfact=km_per_deg_lon(center_lat), yfact=KM_PER_DEG_LAT, origin=poly.centroid
                )
                vertices = len(poly.exterior.coords) - 1

                aois.append(
                    {
                        "aoi_id": aoi_id,
                        "customer_id": cust_id,
                        "aoi_name": f"{region.replace('_', ' ').title()} - {faker.city()}",
                        "region": region,
                        "shape_type": shape,
                        "geometry_wkt": poly.wkt,
                        "area_km2": round(area_km2, 4),
                        "vertex_count": vertices,
                        "compactness": round(compactness(poly_km), 4),
                        "created_at": None,  # backfilled
                    }
                )

                n_orders = max(1, int(rng.poisson(seg_cfg["orders_per_aoi"])))
                for _ in range(n_orders):
                    order_id = f"ORD-{len(orders) + 1:06d}"
                    window_days = int(rng.integers(seg_cfg["window_days"]["min"], seg_cfg["window_days"]["max"] + 1))
                    # Keep the whole window inside the STAC range (decision 4).
                    latest_offset = max(0, total_days - window_days)
                    day_offset = int(rng.integers(0, latest_offset + 1))
                    ordered_at = datetime.combine(start + timedelta(days=day_offset), _random_time(rng), tzinfo=timezone.utc)
                    window_start = ordered_at + timedelta(days=ordering["lead_days"])
                    window_end = window_start + timedelta(days=window_days)
                    priority = "rush" if rng.random() < seg_cfg["rush_share"] else "standard"
                    orders.append(
                        {
                            "order_id": order_id,
                            "customer_id": cust_id,
                            "aoi_id": aoi_id,
                            "ordered_at": ordered_at,
                            "window_start": window_start,
                            "window_end": window_end,
                            "max_cloud_cover": float(seg_cfg["cloud_max"]),
                            "priority": priority,
                            "price_usd": price_order(area_km2, customers[-1]["contract_tier"], priority, segment, pricing),
                            "processing_hours": processing_hours(
                                rng, area_km2, aois[-1]["compactness"], vertices, priority, pt
                            ),
                        }
                    )

    customers_df = pd.DataFrame(customers)
    aois_df = pd.DataFrame(aois)
    orders_df = pd.DataFrame(orders)

    # Backfill created_at (<= earliest order on the AOI) and signup_date
    # (<= earliest created_at for the customer), satisfying the ordering chain
    # signup_date <= created_at <= ordered_at <= window_start < window_end.
    first_order = orders_df.groupby("aoi_id")["ordered_at"].min()
    aois_df["created_at"] = [
        first_order[a] - timedelta(days=int(rng.integers(1, 60))) for a in aois_df["aoi_id"]
    ]
    first_aoi = aois_df.groupby("customer_id")["created_at"].min()
    max_tenure = ordering["max_tenure_days_before_first_order"]
    customers_df["signup_date"] = [
        (first_aoi[c] - timedelta(days=int(rng.integers(1, max_tenure)))).date() for c in customers_df["customer_id"]
    ]

    return {"customers": customers_df, "aois": aois_df, "orders": orders_df}


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #


def write_tables(tables: dict[str, pd.DataFrame], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        path = out_dir / f"{name}.csv"
        df.to_csv(path, index=False)
        logger.info("wrote %d %s rows to %s", len(df), name, path)


def sample_tables(tables: dict[str, pd.DataFrame], n_customers: int) -> dict[str, pd.DataFrame]:
    """A referentially-intact slice: the first N customers and their AOIs/orders,
    committed so the project runs without credentials."""
    cust = tables["customers"].head(n_customers)
    ids = set(cust["customer_id"])
    aois = tables["aois"][tables["aois"]["customer_id"].isin(ids)]
    orders = tables["orders"][tables["orders"]["aoi_id"].isin(set(aois["aoi_id"]))]
    return {"customers": cust, "aois": aois, "orders": orders}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    default_start, default_end = default_window(date.today())
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=42, help="drives numpy + Faker; same seed => identical output")
    parser.add_argument("--start", type=date.fromisoformat, default=default_start, help="order window floor, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default=default_end, help="order window ceiling, YYYY-MM-DD")
    parser.add_argument("--config", type=Path, default=config.SYNTHETIC_CONFIG_PATH)
    parser.add_argument("--aoi-config", type=Path, default=config.AOI_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=config.SYNTHETIC_RAW_DIR)
    parser.add_argument("--sample-dir", type=Path, default=config.SYNTHETIC_SAMPLE_DIR)
    parser.add_argument("--sample-customers", type=int, default=15, help="0 disables writing the committed sample")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    if args.start >= args.end:
        logger.error("--start (%s) must be before --end (%s)", args.start, args.end)
        return 2

    cfg = load_config(args.config)
    aois_cfg = config.load_aois(args.aoi_config)

    logger.info("generating synthetic data (seed=%d, %s..%s)", args.seed, args.start, args.end)
    tables = generate(cfg, aois_cfg, args.seed, args.start, args.end)
    write_tables(tables, args.output_dir)

    if args.sample_customers:
        sample = sample_tables(tables, args.sample_customers)
        write_tables(sample, args.sample_dir)

    counts = {name: len(df) for name, df in tables.items()}
    by_segment = tables["customers"]["segment"].value_counts().to_dict()
    logger.info("totals: %s", counts)
    logger.info("customers by segment: %s", by_segment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
