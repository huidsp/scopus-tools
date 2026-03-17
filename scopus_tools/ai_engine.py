import os
from openai import OpenAI

def estimate_expertise(papers, lang="ja"):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "OpenAI API key not found. Skipping analysis."
    
    client = OpenAI(api_key=api_key)
    # 上位論文と最新論文のタイトルをコンテキストにする
    top_papers = sorted(papers, key=lambda x: x['citations'], reverse=True)[:10]
    titles = [p['title'] for p in top_papers]
    
    prompt = f"""
    The following is a list of research paper titles by a specific researcher:
    {chr(10).join(titles)}

    Based on these titles, please provide:
    1. A concise summary of their primary research field (Expertise).
    2. 3-5 key technical terms that define their work.
    
    Respond in {lang}.
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content