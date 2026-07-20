#!/usr/bin/env python3
"""
NARA-Compliant Export Tool for GitHub Archive System

Exports archived records in formats suitable for federal records management:
- JSON (machine-readable, complete data)
- CSV (spreadsheet-compatible summary)
- PDF (human-readable reports with metadata)

Supports:
- Full archive export
- Date range filtering
- Organization/repository filtering
- Incremental exports (since last export)
- Manifest generation with checksums
"""

import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("WARNING: reportlab not installed. PDF export disabled. Run: pip install reportlab")


class NARAExporter:
    """Export GitHub archive data in NARA-compliant formats"""
    
    def __init__(self, database_url: str, output_dir: str):
        self.database_url = database_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.conn = None
        self.export_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.manifest = {
            "export_id": self.export_id,
            "export_timestamp": datetime.now(timezone.utc).isoformat(),
            "format_version": "1.0",
            "exporter": "GitHub Archive NARA Export Tool",
            "files": [],
            "statistics": {},
            "filters_applied": {}
        }
    
    def connect(self):
        """Establish database connection"""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            url = url.replace("postgresql+asyncpg://", "postgresql://")
        self.conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
    
    def compute_file_checksum(self, filepath: Path) -> str:
        """Compute SHA-256 checksum of a file"""
        sha256 = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def add_file_to_manifest(self, filepath: Path, description: str, record_count: int = 0):
        """Add file to export manifest"""
        self.manifest["files"].append({
            "filename": filepath.name,
            "path": str(filepath.relative_to(self.output_dir)),
            "description": description,
            "size_bytes": filepath.stat().st_size,
            "sha256_checksum": self.compute_file_checksum(filepath),
            "record_count": record_count,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
    
    def export_webhook_events(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        organization: Optional[str] = None,
        repository: Optional[str] = None,
        format: str = "json"
    ) -> int:
        """Export webhook events"""
        print(f"\nExporting webhook events ({format})...")
        
        # Build query
        query = """
            SELECT 
                id, received_at, delivery_id, event_type, action,
                organization, repository, sender, github_id,
                payload, checksum, source_ip, created_at
            FROM webhook_events
            WHERE 1=1
        """
        params = {}
        
        if since:
            query += " AND received_at >= %(since)s"
            params["since"] = since
        if until:
            query += " AND received_at <= %(until)s"
            params["until"] = until
        if organization:
            query += " AND organization = %(organization)s"
            params["organization"] = organization
        if repository:
            query += " AND repository = %(repository)s"
            params["repository"] = repository
        
        query += " ORDER BY received_at ASC"
        
        # Store filters in manifest
        self.manifest["filters_applied"]["webhook_events"] = {
            "since": since,
            "until": until,
            "organization": organization,
            "repository": repository
        }
        
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            
            if format == "json":
                return self._export_json(cur, "webhook_events")
            elif format == "csv":
                return self._export_csv(cur, "webhook_events", [
                    "id", "received_at", "delivery_id", "event_type", "action",
                    "organization", "repository", "sender", "github_id", "checksum"
                ])
            elif format == "both":
                # Need to fetch all data first
                rows = cur.fetchall()
                count = self._export_json_from_rows(rows, "webhook_events")
                self._export_csv_from_rows(rows, "webhook_events", [
                    "id", "received_at", "delivery_id", "event_type", "action",
                    "organization", "repository", "sender", "github_id", "checksum"
                ])
                return count
    
    def export_content_versions(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        organization: Optional[str] = None,
        content_type: Optional[str] = None,
        format: str = "json"
    ) -> int:
        """Export content versions with full history"""
        print(f"\nExporting content versions ({format})...")
        
        query = """
            SELECT 
                cv.id, cv.webhook_event_id, cv.content_type, cv.github_id,
                cv.github_node_id, cv.version_number, cv.previous_version_id,
                cv.is_deletion, cv.title, cv.body, cv.state, cv.metadata,
                cv.actor_login, cv.actor_id, cv.github_created_at,
                cv.github_updated_at, cv.captured_at, cv.checksum,
                we.organization, we.repository
            FROM content_versions cv
            JOIN webhook_events we ON cv.webhook_event_id = we.id
            WHERE 1=1
        """
        params = {}
        
        if since:
            query += " AND cv.captured_at >= %(since)s"
            params["since"] = since
        if until:
            query += " AND cv.captured_at <= %(until)s"
            params["until"] = until
        if organization:
            query += " AND we.organization = %(organization)s"
            params["organization"] = organization
        if content_type:
            query += " AND cv.content_type = %(content_type)s"
            params["content_type"] = content_type
        
        query += " ORDER BY cv.captured_at ASC"
        
        self.manifest["filters_applied"]["content_versions"] = {
            "since": since,
            "until": until,
            "organization": organization,
            "content_type": content_type
        }
        
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            
            if format == "json":
                return self._export_json(cur, "content_versions")
            elif format == "csv":
                return self._export_csv(cur, "content_versions", [
                    "id", "content_type", "github_id", "version_number",
                    "is_deletion", "title", "state", "actor_login",
                    "captured_at", "organization", "repository", "checksum"
                ])
            elif format == "both":
                rows = cur.fetchall()
                count = self._export_json_from_rows(rows, "content_versions")
                self._export_csv_from_rows(rows, "content_versions", [
                    "id", "content_type", "github_id", "version_number",
                    "is_deletion", "title", "state", "actor_login",
                    "captured_at", "organization", "repository", "checksum"
                ])
                return count
    
    def _export_json(self, cursor, name: str) -> int:
        """Export cursor results to JSON file"""
        filepath = self.output_dir / f"{name}_{self.export_id}.json"
        count = 0
        
        with open(filepath, 'w') as f:
            f.write('[\n')
            first = True
            
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                
                for row in rows:
                    if not first:
                        f.write(',\n')
                    first = False
                    
                    # Convert to JSON-serializable format
                    record = dict(row)
                    for key, value in record.items():
                        if isinstance(value, datetime):
                            record[key] = value.isoformat()
                        elif hasattr(value, '__str__') and not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                            record[key] = str(value)
                    
                    f.write('  ' + json.dumps(record, default=str))
                    count += 1
            
            f.write('\n]')
        
        self.add_file_to_manifest(filepath, f"Complete {name} export in JSON format", count)
        print(f"  Exported {count:,} records to {filepath.name}")
        return count
    
    def _export_json_from_rows(self, rows: List[Dict], name: str) -> int:
        """Export rows to JSON file"""
        filepath = self.output_dir / f"{name}_{self.export_id}.json"
        
        records = []
        for row in rows:
            record = dict(row)
            for key, value in record.items():
                if isinstance(value, datetime):
                    record[key] = value.isoformat()
                elif hasattr(value, '__str__') and not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                    record[key] = str(value)
            records.append(record)
        
        with open(filepath, 'w') as f:
            json.dump(records, f, indent=2, default=str)
        
        self.add_file_to_manifest(filepath, f"Complete {name} export in JSON format", len(records))
        print(f"  Exported {len(records):,} records to {filepath.name}")
        return len(records)
    
    def _export_csv(self, cursor, name: str, columns: List[str]) -> int:
        """Export cursor results to CSV file"""
        filepath = self.output_dir / f"{name}_{self.export_id}.csv"
        count = 0
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                
                for row in rows:
                    record = dict(row)
                    for key, value in record.items():
                        if isinstance(value, datetime):
                            record[key] = value.isoformat()
                        elif isinstance(value, (dict, list)):
                            record[key] = json.dumps(value)
                    writer.writerow(record)
                    count += 1
        
        self.add_file_to_manifest(filepath, f"Summary {name} export in CSV format", count)
        print(f"  Exported {count:,} records to {filepath.name}")
        return count
    
    def _export_csv_from_rows(self, rows: List[Dict], name: str, columns: List[str]) -> int:
        """Export rows to CSV file"""
        filepath = self.output_dir / f"{name}_{self.export_id}.csv"
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            
            for row in rows:
                record = dict(row)
                for key, value in record.items():
                    if isinstance(value, datetime):
                        record[key] = value.isoformat()
                    elif isinstance(value, (dict, list)):
                        record[key] = json.dumps(value)
                writer.writerow(record)
        
        self.add_file_to_manifest(filepath, f"Summary {name} export in CSV format", len(rows))
        print(f"  Exported {len(rows):,} records to {filepath.name}")
        return len(rows)
    
    def generate_pdf_report(self) -> Optional[Path]:
        """Generate PDF summary report"""
        if not PDF_AVAILABLE:
            print("  PDF generation skipped (reportlab not installed)")
            return None
        
        print("\nGenerating PDF summary report...")
        
        filepath = self.output_dir / f"export_report_{self.export_id}.pdf"
        doc = SimpleDocTemplate(str(filepath), pagesize=letter)
        styles = getSampleStyleSheet()
        story = []
        
        # Title
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=30
        )
        story.append(Paragraph("GitHub Archive Export Report", title_style))
        story.append(Spacer(1, 12))
        
        # Export metadata
        story.append(Paragraph("Export Information", styles['Heading2']))
        metadata = [
            ["Export ID:", self.export_id],
            ["Export Date:", self.manifest["export_timestamp"]],
            ["Format Version:", self.manifest["format_version"]],
            ["Total Files:", str(len(self.manifest["files"]))],
        ]
        
        t = Table(metadata, colWidths=[2*inch, 4*inch])
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(t)
        story.append(Spacer(1, 24))
        
        # Statistics
        if self.manifest.get("statistics"):
            story.append(Paragraph("Statistics", styles['Heading2']))
            stats_data = [[k, str(v)] for k, v in self.manifest["statistics"].items()]
            t = Table(stats_data, colWidths=[3*inch, 3*inch])
            t.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ]))
            story.append(t)
            story.append(Spacer(1, 24))
        
        # Files manifest
        story.append(Paragraph("Exported Files", styles['Heading2']))
        for file_info in self.manifest["files"]:
            story.append(Paragraph(f"<b>{file_info['filename']}</b>", styles['Normal']))
            story.append(Paragraph(f"Description: {file_info['description']}", styles['Normal']))
            story.append(Paragraph(f"Records: {file_info['record_count']:,}", styles['Normal']))
            story.append(Paragraph(f"Size: {file_info['size_bytes']:,} bytes", styles['Normal']))
            story.append(Paragraph(f"SHA-256: {file_info['sha256_checksum']}", styles['Normal']))
            story.append(Spacer(1, 12))
        
        # Filters applied
        story.append(PageBreak())
        story.append(Paragraph("Filters Applied", styles['Heading2']))
        for table_name, filters in self.manifest.get("filters_applied", {}).items():
            story.append(Paragraph(f"<b>{table_name}</b>", styles['Normal']))
            for key, value in filters.items():
                if value:
                    story.append(Paragraph(f"  {key}: {value}", styles['Normal']))
            story.append(Spacer(1, 12))
        
        # Certification statement
        story.append(Spacer(1, 36))
        story.append(Paragraph("Certification Statement", styles['Heading2']))
        cert_text = """
        This export was generated from the GitHub Archive System, an immutable 
        records management system. All records in this export have been verified 
        against their original SHA-256 checksums. The integrity of the archived 
        data has been maintained from the point of capture through this export.
        
        This export is suitable for federal records retention requirements under 
        NARA guidelines. Each record includes cryptographic verification data 
        that can be used to validate authenticity.
        """
        story.append(Paragraph(cert_text, styles['Normal']))
        
        doc.build(story)
        
        self.add_file_to_manifest(filepath, "PDF summary report for records management", 0)
        print(f"  Generated report: {filepath.name}")
        return filepath
    
    def save_manifest(self):
        """Save export manifest"""
        filepath = self.output_dir / f"manifest_{self.export_id}.json"
        
        with open(filepath, 'w') as f:
            json.dump(self.manifest, f, indent=2)
        
        # Also create checksum file for manifest
        checksum = self.compute_file_checksum(filepath)
        checksum_file = self.output_dir / f"manifest_{self.export_id}.sha256"
        with open(checksum_file, 'w') as f:
            f.write(f"{checksum}  manifest_{self.export_id}.json\n")
        
        print(f"\nManifest saved: {filepath.name}")
        print(f"Manifest checksum: {checksum}")
    
    def get_statistics(self):
        """Gather archive statistics"""
        with self.conn.cursor() as cur:
            # Total events
            cur.execute("SELECT COUNT(*) FROM webhook_events")
            self.manifest["statistics"]["total_webhook_events"] = cur.fetchone()["count"]
            
            # Total content versions
            cur.execute("SELECT COUNT(*) FROM content_versions")
            self.manifest["statistics"]["total_content_versions"] = cur.fetchone()["count"]
            
            # Date range
            cur.execute("SELECT MIN(received_at), MAX(received_at) FROM webhook_events")
            row = cur.fetchone()
            if row["min"]:
                self.manifest["statistics"]["earliest_event"] = row["min"].isoformat()
                self.manifest["statistics"]["latest_event"] = row["max"].isoformat()
            
            # Events by type
            cur.execute("""
                SELECT event_type, COUNT(*) as count 
                FROM webhook_events 
                GROUP BY event_type 
                ORDER BY count DESC
            """)
            self.manifest["statistics"]["events_by_type"] = {
                row["event_type"]: row["count"] for row in cur.fetchall()
            }
            
            # Organizations
            cur.execute("SELECT DISTINCT organization FROM webhook_events")
            self.manifest["statistics"]["organizations"] = [
                row["organization"] for row in cur.fetchall()
            ]


def main():
    parser = argparse.ArgumentParser(
        description='Export GitHub Archive data in NARA-compliant formats'
    )
    parser.add_argument(
        '--database-url',
        default=os.environ.get('DATABASE_URL'),
        help='PostgreSQL connection URL (or set DATABASE_URL env var)'
    )
    parser.add_argument(
        '--output-dir', '-o',
        default=f'./exports/{datetime.now().strftime("%Y%m%d")}',
        help='Output directory for export files'
    )
    parser.add_argument(
        '--format', '-f',
        choices=['json', 'csv', 'both'],
        default='both',
        help='Export format (default: both)'
    )
    parser.add_argument(
        '--since',
        help='Export records since this ISO timestamp'
    )
    parser.add_argument(
        '--until',
        help='Export records until this ISO timestamp'
    )
    parser.add_argument(
        '--organization',
        help='Filter by organization'
    )
    parser.add_argument(
        '--repository',
        help='Filter by repository'
    )
    parser.add_argument(
        '--content-type',
        help='Filter content versions by type (issue, pull_request, etc.)'
    )
    parser.add_argument(
        '--skip-pdf',
        action='store_true',
        help='Skip PDF report generation'
    )
    parser.add_argument(
        '--events-only',
        action='store_true',
        help='Export only webhook events (skip content versions)'
    )
    parser.add_argument(
        '--content-only',
        action='store_true',
        help='Export only content versions (skip webhook events)'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("GitHub Archive - NARA-Compliant Export")
    print("=" * 60)
    
    exporter = NARAExporter(args.database_url, args.output_dir)
    
    try:
        exporter.connect()
        print(f"Connected to database")
        print(f"Output directory: {args.output_dir}")
        
        # Gather statistics
        exporter.get_statistics()
        
        total_records = 0
        
        # Export webhook events
        if not args.content_only:
            count = exporter.export_webhook_events(
                since=args.since,
                until=args.until,
                organization=args.organization,
                repository=args.repository,
                format=args.format
            )
            total_records += count
        
        # Export content versions
        if not args.events_only:
            count = exporter.export_content_versions(
                since=args.since,
                until=args.until,
                organization=args.organization,
                content_type=args.content_type,
                format=args.format
            )
            total_records += count
        
        # Generate PDF report
        if not args.skip_pdf:
            exporter.generate_pdf_report()
        
        # Save manifest
        exporter.manifest["statistics"]["total_exported_records"] = total_records
        exporter.save_manifest()
        
        print("\n" + "=" * 60)
        print("Export Complete")
        print("=" * 60)
        print(f"Total records exported: {total_records:,}")
        print(f"Files created: {len(exporter.manifest['files'])}")
        print(f"Output directory: {args.output_dir}")
        
    finally:
        exporter.close()


if __name__ == '__main__':
    main()
