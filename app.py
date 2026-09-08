import os, sqlite3, smtplib
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, send_from_directory

app=Flask(__name__)
DB="rank_predictor.db"

# Seeded from the Piscipedia report's overall score/rank distribution.
SEED=[(427,1),(396,2),(379,3),(360,4),(349,5),(345,6),(342,7),(340,8),(335,9),(330,11),(327,12),(326,13),(323,14),(322,16),(320,18),(318,19),(316,20),(315,21),(314,22),(312,23),(310,24),(305,25),(301,26),(300,27),(295,28),(290,29),(288,31),(286,32),(285,33),(284,35),(283,37),(282,38),(281,39),(280,40),(278,45),(277,46),(276,48),(275,49),(271,53),(270,55),(268,56),(264,57),(260,58),(259,61),(255,63),(253,64),(252,65),(250,65),(248,70),(245,71),(244,73),(243,74),(240,75),(237,76),(236,77),(230,79),(227,82),(226,83),(222,84),(221,87),(220,88),(218,89),(215,90),(212,91),(211,92),(210,93),(205,95),(200,96),(193,99),(184,100),(183,101),(180,102),(179,103),(156,104),(155,105),(146,106),(145,107),(134,108),(100,109),(90,111),(80,112),(69,113),(8,115)]

def conn():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init():
    c=conn()
    c.execute("""CREATE TABLE IF NOT EXISTS responses(
      id INTEGER PRIMARY KEY, name TEXT, domicile TEXT, application_no TEXT UNIQUE,
      category TEXT, score REAL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.commit(); c.close()

def predict(score):
    data=SEED[:]
    c=conn()
    for r in c.execute("SELECT score FROM responses"):
        data.append((r["score"], None))
    c.close()
    data.sort(key=lambda x:x[0], reverse=True)
    higher=sum(1 for s,_ in data if s>score)
    equal=sum(1 for s,_ in data if s==score)
    # Blend historical report rank with growing live response rank.
    hist=min(SEED, key=lambda x:abs(x[0]-score))[1]
    live=higher+1
    if len(data)<=len(SEED): est=hist
    else: est=round(hist*0.7 + live*0.3)
    spread=max(5,round(est*0.15))
    return est, max(1,est-spread), est+spread, len(data)

def email_submission(d, result):
    host=os.getenv("SMTP_HOST"); user=os.getenv("SMTP_USER"); pwd=os.getenv("SMTP_PASSWORD")
    if not all([host,user,pwd]): return False
    body=f"""New Piscipedia AIEEA PG Rank Predictor response

Name: {d['name']}
Domicile: {d['domicile']}
Application No: {d['application_no']}
Category: {d['category']}
Score: {d['score']}

Predicted AIR: {result[0]}
Range: {result[1]} - {result[2]}
Live dataset size: {result[3]}
"""
    msg=MIMEText(body); msg["Subject"]="New AIEEA PG Rank Predictor Response"
    msg["From"]=user; msg["To"]="oderexdc@gmail.com"
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT","465"))) as s:
        s.login(user,pwd); s.send_message(msg)
    return True

@app.post("/api/predict")
def submit():
    d=request.get_json(force=True)
    for k in ["name","domicile","application_no","category","score"]:
        if not str(d.get(k,"")).strip(): return jsonify(error=f"{k} is required"),400
    try: d["score"]=float(d["score"])
    except: return jsonify(error="Invalid score"),400
    if not 0<=d["score"]<=480: return jsonify(error="Score must be 0–480"),400
    c=conn()
    try:
        c.execute("INSERT INTO responses(name,domicile,application_no,category,score) VALUES(?,?,?,?,?)",
                  (d["name"],d["domicile"],d["application_no"],d["category"],d["score"]))
        c.commit()
    except sqlite3.IntegrityError:
        c.execute("UPDATE responses SET name=?,domicile=?,category=?,score=?,created_at=CURRENT_TIMESTAMP WHERE application_no=?",
                  (d["name"],d["domicile"],d["category"],d["score"],d["application_no"]))
        c.commit()
    c.close()
    result=predict(d["score"])
    try: emailed=email_submission(d,result)
    except Exception as e: emailed=False
    return jsonify(predicted_air=result[0],low=result[1],high=result[2],dataset_size=result[3],email_sent=emailed)

@app.get("/")
def home(): return send_from_directory(".", "index.html")
if __name__=="__main__":
    init(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
