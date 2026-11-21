# Map generation image — NEVER submitted. Runs sqlglot's pinned suite twice
# (contexts coverage pass + read-audit pass) and writes maps to /out.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock pytest-cov
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sg.tar.gz
RUN mkdir /src && python -c "import tarfile,os,shutil,glob; t=tarfile.open('/tmp/sg.tar.gz'); t.extractall('/tmp/x'); src=glob.glob('/tmp/x/*')[0]; [shutil.move(os.path.join(src,f), os.path.join('/src',f)) for f in os.listdir(src)]"
COPY mapgen/read_audit.py /mapgen/read_audit.py
COPY mapgen/gen_map.py /mapgen/gen_map.py
COPY mapgen/run.sh /mapgen/run.sh
COPY mapgen/read_audit.py /mapgen/read_audit.py
COPY mapgen/gen_map.py /mapgen/gen_map.py
COPY mapgen/run.sh /mapgen/run.sh
CMD ["/bin/sh", "/mapgen/run.sh"]
