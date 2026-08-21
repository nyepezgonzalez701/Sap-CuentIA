from fastapi import FastAPI

app = FastAPI(title="Asistente de facturas")

@app.get("/")
def estado():
    return {"mensaje": "El asistente de facturas está funcionando", "estado": "ok"}