import base64, json
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from canonical import canonical_bytes

def jwk_to_verify_key(jwk):
    x = jwk["x"]
    x += "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x)
    return VerifyKey(raw)

def receipt_message(commit_body, receipt):
    inner = {
        "dossierId": receipt["dossierId"],
        "callId": receipt["callId"],
        "action": receipt["action"],
        "accepted": receipt["accepted"],
        "proposalDigest": receipt["proposalDigest"],
        "receiptId": receipt["receiptId"],
    }
    return canonical_bytes({
        "profile": commit_body["profile"],
        "evaluationId": commit_body["evaluationId"],
        "inputDigest": commit_body["inputDigest"],
        "receipt": inner,
    })

def verify_receipt(commit_body, receipt, verifier_jwk):
    sig_b64 = receipt["receiptSignature"]
    sig_b64 += "=" * (-len(sig_b64) % 4)
    signature = base64.b64decode(sig_b64)
    key = jwk_to_verify_key(verifier_jwk)
    key.verify(receipt_message(commit_body, receipt), signature)