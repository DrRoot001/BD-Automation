from module2.embedding.generator import generate_embedding

def embed_text(text: str) -> list[float]:
    res = generate_embedding([text])
    return res[0] if res else []
