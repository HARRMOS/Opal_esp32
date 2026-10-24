# Mot de passe des mises à jour par WiFi = OPAL_DEVICE_TOKEN de include/secrets.h,
# pour ne pas le recopier dans platformio.ini (qui est commité).
import re
from pathlib import Path

Import("env")  # noqa: F821  (fourni par PlatformIO)

secrets = Path(env.subst("$PROJECT_INCLUDE_DIR")) / "secrets.h"  # noqa: F821
match = re.search(r'#define\s+OPAL_DEVICE_TOKEN\s+"([^"]+)"', secrets.read_text(encoding="utf-8"))
if match:
    # Sans --debug : espota afficherait le mot de passe dans le journal.
    flags = [f for f in env.get("UPLOADERFLAGS", []) if f != "--debug"]  # noqa: F821
    env.Replace(UPLOADERFLAGS=flags + ["--auth=" + match.group(1)])  # noqa: F821
else:
    print("ota_auth.py : OPAL_DEVICE_TOKEN introuvable dans include/secrets.h")
