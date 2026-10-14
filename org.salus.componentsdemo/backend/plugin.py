#!/usr/bin/env python3
import logging

import salus_sdk as salus

app = salus.Plugin()
log = logging.getLogger("componentsdemo")


@app.on_frontend_attached
async def attached(frontend_id):
    # The backend knows which frontend component attached.
    component = app.frontend_component(frontend_id)
    await app.send(salus.Payload.str(f"frontend {frontend_id} attached as component '{component}'"),
                   channel="hello", to=frontend_id)


@app.on_frontend_detached
async def detached(frontend_id):
    # With lifetime = "panel" this backend exits after its last frontend detached.
    log.info("frontend %s detached; frontends left: %s", frontend_id, sorted(app.frontends))


if __name__ == "__main__":
    app.run()
