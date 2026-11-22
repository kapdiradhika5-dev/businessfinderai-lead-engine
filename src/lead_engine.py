            lead = prepare_lead(candidate)

            # Check again after website enrichment because
            # website/contact details may reveal duplicates.
            if already_seen(lead, history):
                skipped_history += 1
                continue

            processed.append(lead)

        except Exception as error:

            print(
                f"Could not process {candidate.get('name', 'Unknown')}: "
                f"{error}"
            )

        if index % 50 == 0 or index == len(candidates):

            print(
                f"Processed {index}/{len(candidates)} candidates"
            )

    # Remove duplicates within today's processed candidates.
    processed = remove_duplicate_records(processed)

    # Website opportunity first, then lead score.
    processed.sort(key=dashboard_sort_key)

    selected = processed[:LIMIT]

    # Daily CSV contains only today's selected leads.
    write_csv_file(DAILY_PATH, selected)

    # Save OSM IDs and business signatures for future duplicate checks.
    save_history(selected)

    # Preserve the existing dashboard records.
    old_dashboard = read_csv_file(DASHBOARD_PATH)

    combined_dashboard = remove_duplicate_records(
        old_dashboard + selected
    )

    combined_dashboard.sort(key=dashboard_sort_key)

    write_csv_file(
        DASHBOARD_PATH,
        combined_dashboard
    )

    # Summary
    no_website = sum(
        1 for row in selected
        if row.get("website_status") == "Not listed in OSM"
    )

    high_opportunity = sum(
        1 for row in selected
        if opportunity_rank(row) <= 2
    )

    with_phone = sum(
        1 for row in selected
        if row.get("phone")
    )

    with_email = sum(
        1 for row in selected
        if row.get("email")
    )

    duration = (
        datetime.now(timezone.utc) - started_at
    ).total_seconds()

    print("\n" + "=" * 55)
    print("BUSINESSFINDERAI - DAILY REPORT")
    print("=" * 55)

    print("Candidates sampled:", len(candidates))
    print("Skipped from previous history:", skipped_history)
    print("New unique candidates processed:", len(processed))
    print("Today's selected leads:", len(selected))
    print("Website not listed in OSM:", no_website)
    print("Manual website searches available:", sum(1 for row in selected if row.get("website_search_url")))
    print("High website opportunity:", high_opportunity)
    print("Leads with phone:", with_phone)
    print("Leads with email:", with_email)
    print("Dashboard total:", len(combined_dashboard))
    print(f"Total duration: {duration / 60:.2f} minutes")

    print("\nFiles created:")
    print("-", DAILY_CSV)
    print("-", DASHBOARD_CSV)
    print("-", HISTORY_FILE)

    print("\nBusinessFinderAI run completed.")


if __name__ == "__main__":
    main()
