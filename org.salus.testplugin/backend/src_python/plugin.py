#!/usr/bin/env python3
import salus_sdk as salus

app = salus.Plugin()


@app.channel("chat")
async def chat(msg):
    await app.send(salus.Payload.str("Echo: " + msg.payload.as_str()))


@app.route("GET", "/hello/{name}")
async def hello(req):
    return {"greeting": f"Hello, {req.params['name']}!"}


if __name__ == "__main__":
    app.run()
