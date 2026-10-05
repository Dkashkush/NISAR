# Container for hosting the web app online (Hugging Face Spaces, Render, Fly.io, any Docker host).
FROM python:3.11-slim

# rasterio's wheel needs libexpat, which the slim image leaves out
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces run the container as user 1000; give that user a writable home.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    SARCOMPARE_ONLINE=1 PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user . .

# Build the synthetic demo once so the first visitor does not wait for it
RUN python -c "from sarcompare.demo import make_demo_data; make_demo_data()"

EXPOSE 7860
# Render/Fly set $PORT; Hugging Face uses 7860
CMD ["sh", "-c", "streamlit run app.py --server.port ${PORT:-7860} --server.address 0.0.0.0"]
