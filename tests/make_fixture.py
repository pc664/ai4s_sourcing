"""生成仿 OpenAlex 结构的样例数据，用于离线测试流水线。"""
import json, random
from pathlib import Path
random.seed(0)
inst = [("I1","University of Washington","US","education"),("I2","Generate Biomedicines","US","company"),
        ("I3","Tsinghua University","CN","education"),("I4","ETH Zurich","CH","education"),
        ("I5","Some Bio Startup","GB","company")]
authors = [(f"A{i}", f"Researcher {i}") for i in range(30)]
titles = ["De novo protein design with diffusion models","Antibody affinity maturation via protein language model",
          "Deep learning for enzyme engineering","Graph neural network predicts protein-protein interaction",
          "Protein diffusion in membranes","Machine learning for traffic prediction"]
def inv(text):
    d={}
    for i,w in enumerate(text.split()): d.setdefault(w,[]).append(i)
    return d
works=[]
for k in range(200):
    t=random.choice(titles); y=random.randint(2019,2026)
    au=random.sample(authors, random.randint(2,6))
    works.append({"id":f"https://openalex.org/W{k}","title":t,"publication_year":y,"publication_date":f"{y}-0{random.randint(1,9)}-01",
      "cited_by_count":random.randint(0,500),"doi":None,"abstract_inverted_index":inv(t+" we use a neural network"),
      "primary_location":{"source":{"display_name":"bioRxiv"}},
      "authorships":[{"author":{"id":f"https://openalex.org/{a}","display_name":n,"orcid":None},
        "author_position":"first" if j==0 else ("last" if j==len(au)-1 else "middle"),"is_corresponding":j==len(au)-1,
        "institutions":[dict(zip(["id","display_name","country_code","type"],random.choice(inst)))]} for j,(a,n) in enumerate(au)]})
Path("data").mkdir(exist_ok=True)
with open("data/raw_works.jsonl","w") as f:
    for w in works: f.write(json.dumps(w)+"\n")
print("fixture ok", len(works))
