"""
Receipt extraction - extract merchant, amount, date from emails.

V1: Regex-based extraction
V2 (future): Claude API for intelligent extraction
"""
import re
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class ExtractedReceipt:
    """Extracted receipt data."""
    merchant_name: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    document_date: Optional[date] = None
    category: Optional[str] = None
    confidence: float = 0.0


# Known merchants and their categories
KNOWN_MERCHANTS = {
    # Travel
    "booking.com": ("Booking.com", "travel"),
    "booking": ("Booking.com", "travel"),
    "airbnb": ("Airbnb", "travel"),
    "expedia": ("Expedia", "travel"),
    "hotels.com": ("Hotels.com", "travel"),
    "lufthansa": ("Lufthansa", "travel"),
    "swiss.com": ("Swiss Air", "travel"),
    "sbb.ch": ("SBB", "travel"),
    "deutsche bahn": ("Deutsche Bahn", "travel"),
    "uber": ("Uber", "travel"),
    "lyft": ("Lyft", "travel"),
    "flixbus": ("FlixBus", "travel"),
    "ryanair": ("Ryanair", "travel"),
    "easyjet": ("easyJet", "travel"),
    "kayak": ("Kayak", "travel"),
    
    # Subscriptions
    "spotify": ("Spotify", "subscription"),
    "netflix": ("Netflix", "subscription"),
    "apple.com": ("Apple", "subscription"),
    "google.com": ("Google", "subscription"),
    "microsoft": ("Microsoft", "subscription"),
    "adobe": ("Adobe", "subscription"),
    "dropbox": ("Dropbox", "subscription"),
    "notion": ("Notion", "subscription"),
    "figma": ("Figma", "subscription"),
    "github": ("GitHub", "subscription"),
    "openai": ("OpenAI", "subscription"),
    "anthropic": ("Anthropic", "subscription"),
    
    # Shopping
    "amazon": ("Amazon", "shopping"),
    "ebay": ("eBay", "shopping"),
    "zalando": ("Zalando", "shopping"),
    "digitec": ("Digitec", "shopping"),
    "galaxus": ("Galaxus", "shopping"),
    "ikea": ("IKEA", "shopping"),
    "aliexpress": ("AliExpress", "shopping"),
    
    # Food
    "uber eats": ("Uber Eats", "food"),
    "doordash": ("DoorDash", "food"),
    "just eat": ("Just Eat", "food"),
    "deliveroo": ("Deliveroo", "food"),
    "grubhub": ("Grubhub", "food"),
    "starbucks": ("Starbucks", "food"),
    
    # Office
    "office depot": ("Office Depot", "office"),
    "staples": ("Staples", "office"),
    
    # Utilities
    "swisscom": ("Swisscom", "utilities"),
    "sunrise": ("Sunrise", "utilities"),
    "salt": ("Salt", "utilities"),
}

# Currency patterns
CURRENCY_PATTERNS = [
    (r'€\s*([\d,.]+)', 'EUR'),
    (r'([\d,.]+)\s*€', 'EUR'),
    (r'EUR\s*([\d,.]+)', 'EUR'),
    (r'([\d,.]+)\s*EUR', 'EUR'),
    (r'\$\s*([\d,.]+)', 'USD'),
    (r'([\d,.]+)\s*USD', 'USD'),
    (r'£\s*([\d,.]+)', 'GBP'),
    (r'([\d,.]+)\s*GBP', 'GBP'),
    (r'CHF\s*([\d,.]+)', 'CHF'),
    (r'([\d,.]+)\s*CHF', 'CHF'),
]


def extract_receipt(
    subject: str = None,
    from_email: str = None,
    html: str = None,
) -> Optional[ExtractedReceipt]:
    """
    Extract receipt data from email.
    
    Uses subject line, sender email, and HTML content
    to identify merchant, amount, and category.
    """
    # Combine text for searching
    text = " ".join(filter(None, [subject, from_email, html])).lower()
    
    # Find merchant
    merchant_name = None
    category = None
    confidence = 0.0
    
    for pattern, (name, cat) in KNOWN_MERCHANTS.items():
        if pattern in text:
            merchant_name = name
            category = cat
            confidence = 0.7
            break
    
    # Extract amount
    amount = None
    currency = None
    
    for pattern, curr in CURRENCY_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                amount_str = match.group(1)
                # Handle European format (1.234,56)
                if ',' in amount_str and '.' in amount_str:
                    if amount_str.index(',') > amount_str.index('.'):
                        amount_str = amount_str.replace('.', '').replace(',', '.')
                    else:
                        amount_str = amount_str.replace(',', '')
                else:
                    amount_str = amount_str.replace(',', '.')
                
                amount = float(amount_str)
                currency = curr
                confidence += 0.2
                break
            except ValueError:
                continue
    
    # If we found something, return it
    if merchant_name or amount:
        return ExtractedReceipt(
            merchant_name=merchant_name,
            amount=amount,
            currency=currency,
            category=category,
            confidence=min(confidence, 1.0),
        )
    
    return None


# ===================
# V2: Claude API Extraction (Future)
# ===================
# 
# async def extract_receipt_ai(
#     subject: str,
#     from_email: str,
#     html: str,
# ) -> ExtractedReceipt:
#     """
#     Extract receipt data using Claude API.
#     
#     More accurate than regex, handles edge cases,
#     extracts dates, multiple items, etc.
#     """
#     import anthropic
#     
#     client = anthropic.Anthropic()
#     
#     prompt = f"""Extract receipt/invoice data from this email:
#     
#     Subject: {subject}
#     From: {from_email}
#     
#     Content:
#     {html[:5000]}
#     
#     Return JSON with:
#     - merchant_name: string
#     - amount: number
#     - currency: string (EUR, USD, CHF, etc.)
#     - date: string (YYYY-MM-DD)
#     - category: string (travel, subscription, shopping, food, office, utilities, other)
#     
#     If not a receipt/invoice, return empty object.
#     """
#     
#     response = await client.messages.create(
#         model="claude-sonnet-4-20250514",
#         max_tokens=500,
#         messages=[{"role": "user", "content": prompt}],
#     )
#     
#     # Parse response and return ExtractedReceipt
#     ...
