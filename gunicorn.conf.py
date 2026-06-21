import os

# Render injects the port to listen on via $PORT.
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# Downloads + ffmpeg merging can take a while; give workers room.
timeout = 600

# Free tier = one worker. Keeps memory low and downloads serial.
workers = 1
