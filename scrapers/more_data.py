import base64
import zlib

b64_string = "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"

def decode_flow_v2(compressed_string):
    # Add padding just in case the console truncated it
    compressed_string = compressed_string.strip()
    compressed_string += "=" * ((4 - len(compressed_string) % 4) % 4)
    
    try:
        decoded_bytes = base64.b64decode(compressed_string)
        decompressor = zlib.decompressobj()
        decompressed_text = decompressor.decompress(decoded_bytes).decode('utf-8', errors='ignore')
        
        events = decompressed_text.split('|')
        
        print(f"✅ Successfully extracted {len(events)} market events:\n")
        print("Day | Type   | CPTY   | OptID   | Details")
        print("-" * 80)
        
        for event in events:
            parts = event.split(',')
            if len(parts) < 4: continue
            
            day = int(parts[0])
            action = parts[1]
            
            if action == 'O':
                # Day, O, OptID, ExpiryDays, Strike, FormulaStr
                print(f"D{day:02d} | OPTION | ------ | {parts[2]:<7} | {parts[3]}d Expiry | Strike: {parts[4]:<6} | {parts[5]}")
            elif action == 'R':
                print(f"D{day:02d} | RFQ    | {parts[2]:<6} | {parts[3]:<7} |")
            elif action == 'F' and len(parts) >= 7:
                side = "BUY " if parts[4] == 'B' else "SELL"
                print(f"D{day:02d} | FOK    | {parts[2]:<6} | {parts[3]:<7} | {side} {parts[5]} @ {parts[6]}")
            elif action == 'T' and len(parts) >= 6:
                print(f"D{day:02d} | TRADE  | {parts[2]:<6} | {parts[3]:<7} | Qty: {parts[4]:<3} @ {parts[5]}")
                
    except Exception as e:
        print("❌ Error decoding:", e)

decode_flow_v2(b64_string)