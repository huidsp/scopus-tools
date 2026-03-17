import argparse
import logging
from dotenv import load_dotenv
from scopus_tools import api, core, ai_engine, utils

def main():
    load_dotenv()
    utils.setup_logging()
    
    parser = argparse.ArgumentParser(description="Scopus Data Retrieval & Analysis Tools")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # 1. search (旧 get_author.py)
    search_p = subparsers.add_parser("search", help="Search Scopus ID by name")
    search_p.add_argument("--name", help="Single author name")
    search_p.add_argument("--input", help="Input CSV with 'Name' column")
    search_p.add_argument("--output", help="Output CSV path")

    # 2. stats (旧 get_data.py)
    stats_p = subparsers.add_parser("stats", help="Get paper counts and citations for a year range")
    stats_p.add_argument("--year", required=True, help="Year range like [2020,2025]")
    stats_p.add_argument("--input", required=True, help="Input CSV with 'Scopus ID'")
    stats_p.add_argument("--output", required=True, help="Output CSV path")

    # 3. summary (旧 scopus_summary.py)
    sum_p = subparsers.add_parser("summary", help="Show human-readable summary (H-index, Top 5 papers)")
    sum_p.add_argument("ids", help="Scopus IDs (comma separated)")
    sum_p.add_argument("--years", default=None, help="Year range like [2021,2025]")

    # 4. batch (旧 scopus_batch_summary.py)
    batch_p = subparsers.add_parser("batch", help="Batch generate summary CSV for multiple authors")
    batch_p.add_argument("--input", required=True, help="Input CSV")
    batch_p.add_argument("--output", required=True, help="Output CSV path")

    # 5. analyze (追加機能: OpenAI連携)
    analyze_p = subparsers.add_parser("analyze", help="AI-based expertise estimation")
    analyze_p.add_argument("ids", help="Scopus IDs")
    analyze_p.add_argument("--lang", default="ja", help="Output language")

    args = parser.parse_args()

    if args.command == "search":
        client = api.ScopusClient()
        if args.name:
            results = client.search_author_by_name(args.name)
            utils.print_author_results(args.name, results)
        elif args.input and args.output:
            utils.process_author_csv(args.input, args.output, client)

    elif args.command == "stats":
        client = api.ScopusClient()
        start_y, end_y = map(int, args.year.strip("[]").split(","))
        df = utils.read_input_csv(args.input)
        results = []
        for _, row in df.iterrows():
            scopus_id_value = row.get("Scopus ID")
            if not scopus_id_value or str(scopus_id_value) == "nan":
                logging.warning("Missing Scopus ID for %s, skipping.", row.get("Name", ""))
                continue
            s_ids = str(scopus_id_value).split(",")
            paper_data = client.get_papers_by_year(s_ids, start_y, end_y)
            results.append({**row.to_dict(), **paper_data})
        utils.save_output_csv(results, args.output)

    elif args.command == "summary":
        client = api.ScopusClient()
        s_ids = args.ids.split(",")
        papers = client.search_papers(s_ids)
        first, last = client.get_author_profile(s_ids[0])
        year_range = None
        if args.years:
            try:
                years_text = args.years.strip()
                if not (years_text.startswith("[") and years_text.endswith("]")):
                    raise ValueError
                start_y, end_y = [int(y.strip()) for y in years_text[1:-1].split(",")]
                if start_y > end_y:
                    raise ValueError
                year_range = (start_y, end_y)
            except ValueError:
                parser.error("--years must be like [2021,2025] and start <= end")

        if year_range:
            report = core.summarize_papers(papers, year_range=year_range)
        else:
            report = core.summarize_papers(papers)
        utils.print_report_text(first, last, s_ids, report, papers, year_range=year_range)

    elif args.command == "batch":
        client = api.ScopusClient()
        utils.process_batch_summary(args.input, args.output, client)

    elif args.command == "analyze":
        client = api.ScopusClient()
        papers = client.search_papers(args.ids.split(","))
        print(ai_engine.estimate_expertise(papers, lang=args.lang))

if __name__ == "__main__":
    main()