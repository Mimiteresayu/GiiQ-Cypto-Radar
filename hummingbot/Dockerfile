# C48-2 Hummingbot HL testnet perpetual MM image.
# Official image, pinned by digest (Docker Hub tag version-2.16.0 = git tag v2.16.0 @ 8f19061).
# Build context: this hummingbot/ directory (Railway root directory = hummingbot).
FROM hummingbot/hummingbot:version-2.16.0@sha256:e222f070d42814013fb5ea7fe537926f790b259512950369da1e15a69dcbd38f

ENV HEADLESS_MODE=true \
    CONFIG_FILE_NAME=conf_perpetual_market_making_c48_2.yml

WORKDIR /home/hummingbot

COPY conf_perpetual_market_making_c48_2.yml assert_locks.py giiq_entrypoint.py /home/hummingbot/giiq/

# One Railway volume at /home/hummingbot/data holds the trade SQLite and, via this symlink, the logs.
RUN rm -rf /home/hummingbot/logs \
    && ln -s /home/hummingbot/data/logs /home/hummingbot/logs \
    && /opt/conda/envs/hummingbot/bin/python /home/hummingbot/giiq/giiq_entrypoint.py --check-config

VOLUME ["/home/hummingbot/data"]

ENTRYPOINT ["/opt/conda/envs/hummingbot/bin/python", "/home/hummingbot/giiq/giiq_entrypoint.py"]
CMD []
