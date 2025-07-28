from app import app, db, Owner
from werkzeug.security import check_password_hash

with app.app_context():
    o = Owner.query.filter_by(username='owner').first()
    print("Hash:", o.password_hash)
    print("Password match (PrincessRF):", check_password_hash(o.password_hash, 'PrincessRF'))
