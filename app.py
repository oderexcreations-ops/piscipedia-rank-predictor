import os, re, sqlite3, smtplib, secrets
from email.message import EmailMessage
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from PyPDF2 import PdfReader

from seed_data import RECORDS

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DB_PATH = BASE / os.getenv('DATABASE_PATH','piscipedia.db')
UPLOAD_DIR = BASE / 'uploads'; UPLOAD_DIR.mkdir(exist_ok=True)
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=['200 per day','60 per hour'])

CATEGORIES=['General / UR','OBC-NCL','EWS','SC','ST','Other']
STATES=['Andhra Pradesh','Arunachal Pradesh','Assam','Bihar','Chhattisgarh','Goa','Gujarat','Haryana','Himachal Pradesh','Jharkhand','Karnataka','Kerala','Madhya Pradesh','Maharashtra','Manipur','Meghalaya','Mizoram','Nagaland','Odisha','Punjab','Rajasthan','Sikkim','Tamil Nadu','Telangana','Tripura','Uttar Pradesh','Uttarakhand','West Bengal','Andaman and Nicobar Islands','Chandigarh','Dadra and Nagar Haveli and Daman and Diu','Delhi','Jammu and Kashmir','Ladakh','Lakshadweep','Puducherry']

SCHEMA='''
CREATE TABLE IF NOT EXISTS datasets(id INTEGER PRIMARY KEY, version INTEGER UNIQUE, name TEXT, source_document TEXT, record_count INTEGER, status TEXT, created_at TEXT, activated_at TEXT);
CREATE TABLE IF NOT EXISTS historical_records(id INTEGER PRIMARY KEY, marks REAL NOT NULL, rank INTEGER NOT NULL, category TEXT, domicile_state TEXT, exam_year INTEGER, program TEXT, source_document TEXT, source_page INTEGER, dataset_version INTEGER, verified INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, candidate_name TEXT NOT NULL, marks REAL NOT NULL, category TEXT NOT NULL, other_category TEXT, domicile_state TEXT NOT NULL, predicted_rank REAL, rank_lower REAL, rank_upper REAL, confidence TEXT, match_count INTEGER, algorithm_version TEXT, dataset_version INTEGER, consent INTEGER NOT NULL, email_status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS verified_results(id INTEGER PRIMARY KEY, submission_id INTEGER UNIQUE, actual_rank INTEGER, verification_source TEXT, notes TEXT, verified_at TEXT, verified_by TEXT);
CREATE TABLE IF NOT EXISTS states(id INTEGER PRIMARY KEY, state_name TEXT UNIQUE, state_code TEXT, active INTEGER DEFAULT 1);
'''

def db():
    c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); c.executescript(SCHEMA)
    if c.execute('SELECT COUNT(*) n FROM states').fetchone()['n']==0:
        c.executemany('INSERT INTO states(state_name,state_code) VALUES(?,?)',[(s,'') for s in STATES])
    if c.execute('SELECT COUNT(*) n FROM datasets').fetchone()['n']==0:
        now=datetime.now(timezone.utc).isoformat(); c.execute('INSERT INTO datasets(version,name,source_document,record_count,status,created_at,activated_at) VALUES(1,?,?,?,?,?,?)',(1,'Piscipedia Historical Dataset','ICAR_AIEEA_PG_2026_Final_Ranks.pdf',len(RECORDS),'active',now,now))
        c.executemany('INSERT INTO historical_records(marks,rank,program,source_document,dataset_version,created_at) VALUES(?,?,?,?,?,?)',[(m,r,'Fisheries','ICAR_AIEEA_PG_2026_Final_Ranks.pdf',1,now) for m,r in RECORDS])
    c.commit(); c.close()

def admin_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get('admin'): return redirect(url_for('admin_login'))
        return f(*a,**kw)
    return w

def predict(marks, category, state):
    c=db(); rows=c.execute('SELECT marks,rank,category,domicile_state FROM historical_records WHERE dataset_version=(SELECT version FROM datasets WHERE status="active" ORDER BY version DESC LIMIT 1) ORDER BY marks DESC').fetchall(); c.close()
    if not rows: return None
    # Exact historical scores take the exact rank. Otherwise use linear interpolation between nearest score/rank observations.
    exact=[r for r in rows if abs(float(r['marks'])-marks)<1e-9]
    if exact:
        rank=float(exact[0]['rank']); lo=max(1,rank-5); hi=rank+5; conf='High'
        used=min(10,len(exact))
        return rank,lo,hi,conf,used
    ordered=sorted(rows,key=lambda r:abs(float(r['marks'])-marks))[:8]
    ordered=sorted(ordered,key=lambda r:float(r['marks']), reverse=True)
    above=[r for r in rows if float(r['marks'])>marks]
    below=[r for r in rows if float(r['marks'])<marks]
    if above and below:
        a=min(above,key=lambda r:float(r['marks'])-marks); b=max(below,key=lambda r:marks-float(r['marks']))
        x1,x2=float(a['marks']),float(b['marks']); y1,y2=float(a['rank']),float(b['rank'])
        rank=y1+(marks-x1)*(y2-y1)/(x2-x1)
    else:
        rank=float(ordered[0]['rank'])
    ranks=[float(r['rank']) for r in ordered]; spread=max(ranks)-min(ranks) if ranks else 20
    distance=abs(float(ordered[0]['marks'])-marks)
    conf='High' if len(rows)>=30 and distance<=2 else ('Moderate' if len(rows)>=10 and distance<=8 else 'Low')
    half=max(10,round(spread*0.35)); lo=max(1,round(rank-half)); hi=round(rank+half)
    return round(rank),lo,hi,conf,len(ordered)

def send_notification(data):
    recipient=os.getenv('NOTIFICATION_EMAIL','oderexcreations@gmail.com'); host=os.getenv('SMTP_HOST')
    if not host: return 'not_configured'
    msg=EmailMessage(); msg['Subject']='New Piscipedia ICAR AIEEA PG Rank Prediction'; msg['From']=os.getenv('SMTP_USERNAME',recipient); msg['To']=recipient
    msg.set_content('\n'.join(f'{k}: {v}' for k,v in data.items()))
    with smtplib.SMTP(host,int(os.getenv('SMTP_PORT','587')),timeout=15) as s:
        if os.getenv('SMTP_USE_TLS','true').lower()=='true': s.starttls()
        user=os.getenv('SMTP_USERNAME'); pwd=os.getenv('SMTP_PASSWORD')
        if user and pwd: s.login(user,pwd)
        s.send_message(msg)
    return 'sent'

@app.route('/')
def home(): return render_template('home.html')
@app.route('/predict',methods=['GET','POST'])
@limiter.limit('20 per hour')
def predict_page():
    if request.method=='GET': return render_template('predict.html',categories=CATEGORIES,states=STATES)
    d=request.form
    name=d.get('name','').strip(); category=d.get('category',''); other=d.get('other_category','').strip(); state=d.get('state',''); consent=d.get('consent')
    try: marks=float(d.get('marks',''))
    except: return render_template('predict.html',categories=CATEGORIES,states=STATES,error='Please enter valid ICAR AIEEA PG marks.')
    max_marks=float(os.getenv('MAX_MARKS','400'))
    if not name: err='Please enter your name.'
    elif marks<0 or marks>max_marks: err=f'Please enter marks between 0 and {max_marks:g}.'
    elif category not in CATEGORIES: err='Please select your category.'
    elif category=='Other' and not other: err='Please specify your category.'
    elif state not in STATES: err='Please select your domicile state/UT.'
    elif not consent: err='Please accept the consent statement.'
    else: err=None
    if err: return render_template('predict.html',categories=CATEGORIES,states=STATES,error=err)
    result=predict(marks,category,state)
    if not result: return render_template('predict.html',categories=CATEGORIES,states=STATES,error='There is not enough historical data to calculate a prediction.')
    rank,lo,hi,conf,count=result; c=db(); ds=c.execute('SELECT version FROM datasets WHERE status="active" ORDER BY version DESC LIMIT 1').fetchone()['version']; now=datetime.now(timezone.utc).isoformat()
    cur=c.execute('INSERT INTO submissions(candidate_name,marks,category,other_category,domicile_state,predicted_rank,rank_lower,rank_upper,confidence,match_count,algorithm_version,dataset_version,consent,email_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,marks,category,other,state,rank,lo,hi,conf,count,'historical-interpolation-v1',ds,1,'pending',now)); sid=cur.lastrowid; c.commit(); c.close()
    status='not_configured'
    try: status=send_notification({'Submission ID':sid,'Candidate Name':name,'Marks':marks,'Category':category,'Domicile':state,'Predicted Rank':rank,'Rank Range':f'{lo}–{hi}','Confidence':conf,'Historical Records Used':count,'Dataset Version':ds,'Algorithm Version':'historical-interpolation-v1','Timestamp':now})
    except Exception: status='failed'
    c=db(); c.execute('UPDATE submissions SET email_status=? WHERE id=?',(status,sid)); c.commit(); c.close()
    return redirect(url_for('result',sid=sid))

@app.route('/result/<int:sid>')
def result(sid):
    c=db(); r=c.execute('SELECT * FROM submissions WHERE id=?',(sid,)).fetchone(); c.close()
    if not r: return 'Result not found',404
    return render_template('result.html',r=r)

@app.route('/how-it-works')
def how(): return render_template('how.html')
@app.route('/faq')
def faq(): return render_template('faq.html')
@app.route('/about')
def about(): return render_template('about.html')
@app.route('/privacy')
def privacy(): return render_template('privacy.html')
@app.route('/terms')
def terms(): return render_template('terms.html')

@app.route('/admin/login',methods=['GET','POST'])
def admin_login():
    if request.method=='POST':
        if secrets.compare_digest(request.form.get('email',''),os.getenv('ADMIN_EMAIL','admin@example.com')) and secrets.compare_digest(request.form.get('password',''),os.getenv('ADMIN_PASSWORD','change-me')):
            session['admin']=True; return redirect(url_for('admin'))
        flash('Invalid administrator credentials.','error')
    return render_template('admin_login.html')
@app.route('/admin/logout')
def admin_logout(): session.clear(); return redirect(url_for('home'))

@app.route('/admin')
@admin_required
def admin():
    c=db(); stats={'predictions':c.execute('SELECT COUNT(*) n FROM submissions').fetchone()['n'],'historical':c.execute('SELECT COUNT(*) n FROM historical_records').fetchone()['n'],'verified':c.execute('SELECT COUNT(*) n FROM verified_results').fetchone()['n'],'datasets':c.execute('SELECT COUNT(*) n FROM datasets').fetchone()['n']}; subs=c.execute('SELECT s.*,v.actual_rank FROM submissions s LEFT JOIN verified_results v ON v.submission_id=s.id ORDER BY s.id DESC LIMIT 100').fetchall(); datasets=c.execute('SELECT * FROM datasets ORDER BY version DESC').fetchall(); c.close(); return render_template('admin.html',stats=stats,subs=subs,datasets=datasets)

@app.route('/admin/upload',methods=['POST'])
@admin_required
def upload_pdf():
    f=request.files.get('pdf')
    if not f or not f.filename.lower().endswith('.pdf'): flash('Please upload a PDF.','error'); return redirect(url_for('admin'))
    path=UPLOAD_DIR / re.sub(r'[^A-Za-z0-9._-]','_',f.filename); f.save(path)
    try:
        text='\n'.join((p.extract_text() or '') for p in PdfReader(str(path)).pages)
        pairs=[]
        for m,r in re.findall(r'(?m)(\d+(?:\.\d+)?)\s+(\d+)\b',text):
            pairs.append((float(m),int(r)))
        # Prefer rows that resemble the supplied score/rank structure and deduplicate.
        pairs=list(dict.fromkeys(pairs))
        if not pairs: raise ValueError('No score/rank pairs detected.')
        c=db(); newv=(c.execute('SELECT COALESCE(MAX(version),0) m FROM datasets').fetchone()['m'] or 0)+1; now=datetime.now(timezone.utc).isoformat()
        c.execute('UPDATE datasets SET status="inactive" WHERE status="active"'); c.execute('INSERT INTO datasets(version,name,source_document,record_count,status,created_at,activated_at) VALUES(?,?,?,?,?,?,?)',(newv,f'Piscipedia Dataset v{newv}',f.filename,len(pairs),'active',now,now))
        c.executemany('INSERT INTO historical_records(marks,rank,program,source_document,dataset_version,created_at) VALUES(?,?,?,?,?,?)',[(m,r,'Fisheries',f.filename,newv,now) for m,r in pairs]); c.commit(); c.close(); flash(f'Imported {len(pairs)} records as Dataset v{newv}.','success')
    except Exception as e: flash(f'PDF import failed: {e}','error')
    return redirect(url_for('admin'))

@app.route('/admin/verify/<int:sid>',methods=['POST'])
@admin_required
def verify(sid):
    try: actual=int(request.form['actual_rank'])
    except: flash('Actual rank must be an integer.','error'); return redirect(url_for('admin'))
    c=db(); c.execute('INSERT OR REPLACE INTO verified_results(submission_id,actual_rank,verification_source,notes,verified_at,verified_by) VALUES(?,?,?,?,?,?)',(sid,actual,'Administrator','',datetime.now(timezone.utc).isoformat(),os.getenv('ADMIN_EMAIL','admin'))); c.commit(); c.close(); flash('Verified result saved.','success'); return redirect(url_for('admin'))

@app.route('/admin/export')
@admin_required
def export_csv():
    import csv
    from io import StringIO
    c=db(); rows=c.execute('SELECT * FROM submissions ORDER BY id').fetchall(); c.close(); out=StringIO(); w=csv.writer(out); w.writerow(rows[0].keys() if rows else ['id']); [w.writerow(list(r)) for r in rows]; from flask import Response; return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=piscipedia_submissions.csv'})

@app.context_processor
def globals(): return {'brand':'Piscipedia'}

if __name__=='__main__':
    init_db(); app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
else: init_db()
