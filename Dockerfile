# Blast Radius entry — scorer builds this on linux/amd64 and runs it with no
# network. Keep the image digest: its Python and SQLite decide which sqlglot
# tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# sqlglot checkout at the pinned commit (explicitly allowed by the kit rules).
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sg.tar.gz
RUN mkdir /src && python -c "import tarfile,os,shutil,glob; t=tarfile.open('/tmp/sg.tar.gz'); t.extractall('/tmp/x'); src=glob.glob('/tmp/x/*')[0]; [shutil.move(os.path.join(src,f), os.path.join('/src',f)) for f in os.listdir(src)]"

WORKDIR /app
COPY selector.py /app/selector.py
COPY maps /app/maps

CMD ["python", "/app/selector.py"]
