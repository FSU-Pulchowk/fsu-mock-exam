from flask import Flask,request,jsonify
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)
@app.get('/getQuestions')
def giveform():
    with open("sets_new.json",'r') as q:
        return json.load(q)

@app.get('/getAnswers')
def giveanswers():
    with open("answers.json","r") as a:
        return json.load(a)
if __name__ == "__main__":
    app.run(debug=True)
