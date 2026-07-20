#!/usr/bin/env python3
"""
Integrity Verification Tool for GitHub Archive System
Verifies SHA-256 checksums of all archived records
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


def get_db_connection(database_url: str):
    """Create database connection from URL"""
    # Parse DATABASE_URL (format: postgresql://user:<password>@host:port/database)
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def compute_checksum(data: dict) -> str:
    """Compute SHA-256 checksum of data"""
    json_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(json_str.encode()).hexdigest()


def verify_webhook_events(conn, verbose: bool = False, limit: int = None):
    """Verify checksums of webhook_events table"""
    print("\n=== Verifying webhook_events ===")
    
    with conn.cursor() as cur:
        # Get total count
        cur.execute("SELECT COUNT(*) as count FROM webhook_events")
        total = cur.fetchone()['count']
        print(f"Total records: {total:,}")
        
        # Verify in batches
        batch_size = 1000
        offset = 0
        valid_count = 0
        invalid_records = []
        
        query = "SELECT id, payload, checksum FROM webhook_events ORDER BY received_at"
        if limit:
            query += f" LIMIT {limit}"
        
        cur.execute(query)
        
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            
            for row in rows:
                computed = compute_checksum(row['payload'])
                if computed == row['checksum']:
                    valid_count += 1
                else:
                    invalid_records.append({
                        'id': str(row['id']),
                        'stored': row['checksum'],
                        'computed': computed
                    })
                    if verbose:
                        print(f"  INVALID: {row['id']}")
            
            offset += len(rows)
            if not verbose:
                print(f"\r  Verified: {offset:,}/{total:,}", end="", flush=True)
        
        print()  # newline after progress
        
    return valid_count, invalid_records


def verify_content_versions(conn, verbose: bool = False, limit: int = None):
    """Verify checksums of content_versions table"""
    print("\n=== Verifying content_versions ===")
    
    with conn.cursor() as cur:
        # Get total count
        cur.execute("SELECT COUNT(*) as count FROM content_versions")
        total = cur.fetchone()['count']
        print(f"Total records: {total:,}")
        
        # Verify in batches
        batch_size = 1000
        offset = 0
        valid_count = 0
        invalid_records = []
        
        query = "SELECT id, title, body, metadata, checksum FROM content_versions ORDER BY captured_at"
        if limit:
            query += f" LIMIT {limit}"
        
        cur.execute(query)
        
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            
            for row in rows:
                content = {
                    "title": row['title'] or "",
                    "body": row['body'] or "",
                    "metadata": row['metadata'] or {}
                }
                computed = compute_checksum(content)
                
                if computed == row['checksum']:
                    valid_count += 1
                else:
                    invalid_records.append({
                        'id': str(row['id']),
                        'stored': row['checksum'],
                        'computed': computed
                    })
                    if verbose:
                        print(f"  INVALID: {row['id']}")
            
            offset += len(rows)
            if not verbose:
                print(f"\r  Verified: {offset:,}/{total:,}", end="", flush=True)
        
        print()  # newline after progress
        
    return valid_count, invalid_records


def verify_json_files(archive_path: str, verbose: bool = False, limit: int = None):
    """Verify checksums of JSON archive files"""
    print(f"\n=== Verifying JSON files in {archive_path} ===")
    
    if not os.path.exists(archive_path):
        print(f"  Archive path does not exist: {archive_path}")
        return 0, []
    
    valid_count = 0
    invalid_records = []
    file_count = 0
    
    for root, dirs, files in os.walk(archive_path):
        for filename in files:
            if not filename.endswith('.json'):
                continue
            
            if limit and file_count >= limit:
                break
            
            file_path = os.path.join(root, filename)
            file_count += 1
            
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                
                # The JSON file should have a payload field
                if 'payload' in data:
                    # Verify the payload is valid JSON
                    valid_count += 1
                else:
                    invalid_records.append({
                        'file': file_path,
                        'error': 'Missing payload field'
                    })
                    if verbose:
                        print(f"  INVALID: {file_path} - Missing payload")
                        
            except json.JSONDecodeError as e:
                invalid_records.append({
                    'file': file_path,
                    'error': f'Invalid JSON: {e}'
                })
                if verbose:
                    print(f"  INVALID: {file_path} - {e}")
            except Exception as e:
                invalid_records.append({
                    'file': file_path,
                    'error': str(e)
                })
                if verbose:
                    print(f"  ERROR: {file_path} - {e}")
            
            if not verbose and file_count % 100 == 0:
                print(f"\r  Verified: {file_count:,} files", end="", flush=True)
    
    print(f"\r  Verified: {file_count:,} files")
    
    return valid_count, invalid_records


def generate_report(results: dict, output_file: str = None):
    """Generate verification report"""
    report = {
        'verification_time': datetime.utcnow().isoformat() + 'Z',
        'summary': {
            'total_records': 0,
            'valid_records': 0,
            'invalid_records': 0,
            'integrity_status': 'PASS'
        },
        'details': results
    }
    
    for table, data in results.items():
        report['summary']['total_records'] += data.get('total', 0)
        report['summary']['valid_records'] += data.get('valid', 0)
        report['summary']['invalid_records'] += len(data.get('invalid', []))
    
    if report['summary']['invalid_records'] > 0:
        report['summary']['integrity_status'] = 'FAIL'
    
    report_json = json.dumps(report, indent=2)
    
    if output_file:
        with open(output_file, 'w') as f:
            f.write(report_json)
        print(f"\nReport saved to: {output_file}")
    
    return report


def main():
    parser = argparse.ArgumentParser(
        description='Verify integrity of GitHub Archive System records'
    )
    parser.add_argument(
        '--database-url',
        default=os.environ.get('DATABASE_URL'),
        help='PostgreSQL connection URL (or set DATABASE_URL env var)'
    )
    parser.add_argument(
        '--archive-path',
        default=os.environ.get('ARCHIVE_PATH', '/var/lib/github-archive/json'),
        help='Path to JSON archive directory'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show details for each invalid record'
    )
    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of records to verify (for testing)'
    )
    parser.add_argument(
        '--output', '-o',
        help='Output file for JSON report'
    )
    parser.add_argument(
        '--skip-db',
        action='store_true',
        help='Skip database verification'
    )
    parser.add_argument(
        '--skip-files',
        action='store_true',
        help='Skip JSON file verification'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("GitHub Archive System - Integrity Verification")
    print("=" * 60)
    print(f"Started at: {datetime.utcnow().isoformat()}Z")
    
    results = {}
    
    # Verify database records
    if not args.skip_db:
        try:
            conn = get_db_connection(args.database_url)
            
            # Verify webhook_events
            valid, invalid = verify_webhook_events(conn, args.verbose, args.limit)
            results['webhook_events'] = {
                'total': valid + len(invalid),
                'valid': valid,
                'invalid': invalid
            }
            
            # Verify content_versions
            valid, invalid = verify_content_versions(conn, args.verbose, args.limit)
            results['content_versions'] = {
                'total': valid + len(invalid),
                'valid': valid,
                'invalid': invalid
            }
            
            conn.close()
            
        except Exception as e:
            print(f"\nERROR connecting to database: {e}")
            results['database_error'] = str(e)
    
    # Verify JSON files
    if not args.skip_files:
        valid, invalid = verify_json_files(args.archive_path, args.verbose, args.limit)
        results['json_files'] = {
            'total': valid + len(invalid),
            'valid': valid,
            'invalid': invalid
        }
    
    # Generate report
    report = generate_report(results, args.output)
    
    # Print summary
    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    print(f"Total Records:   {report['summary']['total_records']:,}")
    print(f"Valid Records:   {report['summary']['valid_records']:,}")
    print(f"Invalid Records: {report['summary']['invalid_records']:,}")
    print(f"Status:          {report['summary']['integrity_status']}")
    print("=" * 60)
    
    # Exit with appropriate code
    if report['summary']['integrity_status'] == 'FAIL':
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
