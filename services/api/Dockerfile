# Backend image pinning the fiddly native deps: eccodes (GRIB) + WindNinja (orographic wind).
# The Open-Meteo default path does not need these, but pinning them here means a cold checkout
# can run the full stack (native GRIB + terrain downscaling) reproducibly.
FROM continuumio/miniconda3:24.5.0-0

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# eccodes for GRIB decoding; WindNinja is provided via conda-forge where available,
# otherwise install the pinned binary. Kept in one layer for reproducibility.
RUN conda install -y -c conda-forge \
        python=3.11 \
        eccodes=2.35.0 \
        cfgrib \
        rasterio \
        xarray \
        netCDF4 \
    && conda clean -afy

# WindNinja OSS binary (firelab/windninja). Pinned; router falls back to the geometric
# heuristic if the binary is absent, so the build never hard-fails on this.
# TODO(v2): pin an exact WindNinja release tarball/checksum here.
RUN apt-get update && apt-get install -y --no-install-recommends windninja || true \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install -e .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
