"""
Test script - Email to PDF conversion.

Run: python tests/manual_conversion.py

Tests the core conversion without database, Resend, or Stripe.
"""
import asyncio
import sys
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.parse import parse_email
from app.services.pdf import render_pdf, close_browser
from app.services.receipt import extract_receipt


# Sample email payload (like Resend webhook would send)
SAMPLE_EMAIL = {
    "messageId": "test-123",
    "from": "Booking.com <noreply@booking.com>",
    "to": "do@backaspdf.com",
    "subject": "Your booking confirmation - Hotel & Spa Resort Zürich",
    "html": """
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; }
            .header { background: #003580; color: white; padding: 20px; }
            .content { padding: 20px; }
            .price { font-size: 24px; color: #003580; font-weight: bold; }
            .details { background: #f5f5f5; padding: 15px; margin: 15px 0; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Booking.com</h1>
        </div>
        <div class="content">
            <h2>Booking Confirmation</h2>
            <p>Dear Guest,</p>
            <p>Thank you for your reservation!</p>
            
            <div class="details">
                <h3>Hotel & Spa Resort Zürich</h3>
                <p>📍 Bahnhofstrasse 123, 8001 Zürich, Switzerland</p>
                <p>📅 Check-in: January 25, 2025</p>
                <p>📅 Check-out: January 27, 2025</p>
                <p>👤 2 Adults, 1 Room</p>
            </div>
            
            <p class="price">Total: CHF 450.00</p>
            
            <p>Your confirmation number: <strong>1234567890</strong></p>
            
            <hr>
            <p style="color: #666; font-size: 12px;">
                This email was sent by Booking.com. 
                Please do not reply to this email.
            </p>
        </div>
    </body>
    </html>
    """,
    "text": "Booking confirmation for Hotel Zürich. Total: CHF 450.00",
    "attachments": []
}


async def main():
    print("=" * 50)
    print("BackasPDF - Test Conversion")
    print("=" * 50)
    
    try:
        # Step 1: Parse email
        print("\n📧 Step 1: Parsing email...")
        parsed = parse_email(SAMPLE_EMAIL)
        print(f"   From: {parsed.from_email}")
        print(f"   Subject: {parsed.subject}")
        print(f"   HTML length: {len(parsed.html)} chars")
        
        # Step 2: Extract receipt data
        print("\n🧾 Step 2: Extracting receipt data...")
        receipt = extract_receipt(
            subject=parsed.subject,
            from_email=parsed.from_email,
            html=parsed.html,
        )
        if receipt:
            print(f"   Merchant: {receipt.merchant_name}")
            print(f"   Amount: {receipt.amount} {receipt.currency}")
            print(f"   Category: {receipt.category}")
            print(f"   Confidence: {receipt.confidence:.0%}")
        else:
            print("   No receipt data extracted")
        
        # Step 3: Render PDF
        print("\n📄 Step 3: Rendering PDF...")
        print("   (Starting Playwright browser, this may take a moment...)")
        pdf_bytes = await render_pdf(parsed.html)
        print(f"   PDF size: {len(pdf_bytes):,} bytes ({len(pdf_bytes)/1024:.1f} KB)")
        
        # Step 4: Save PDF
        output_path = Path("test_output.pdf")
        output_path.write_bytes(pdf_bytes)
        print(f"\n✅ Success! PDF saved to: {output_path.absolute()}")
        
        # Generate filename like production would
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d")
        if receipt and receipt.merchant_name:
            filename = f"{date_str}_{receipt.merchant_name}_{receipt.amount:.0f}{receipt.currency}.pdf"
        else:
            filename = f"{date_str}_document.pdf"
        print(f"   Production filename would be: {filename}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        # Cleanup
        print("\n🧹 Cleaning up browser...")
        await close_browser()
    
    print("\n" + "=" * 50)
    print("Test completed! Open test_output.pdf to see the result.")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)