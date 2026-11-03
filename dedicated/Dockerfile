# PlaceTogether Dedicated Server Dockerfile
FROM python:3.12-alpine

LABEL maintainer="PlaceTogether"
LABEL description="24/7 Dedicated Server for Roblox Studio Collaborative Building"

WORKDIR /app

# Copy server code
COPY server.py /app/server.py

# Create directory for persistent data volume
RUN mkdir -p /app/data && chown -R nobody:nogroup /app

# Run as non-root user for security
USER nobody

# Expose synchronization port
EXPOSE 8787

# Store database in /app/data/placetogether.db
VOLUME ["/app/data"]

ENTRYPOINT ["python3", "-u", "/app/server.py"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--db", "/app/data/placetogether.db"]
