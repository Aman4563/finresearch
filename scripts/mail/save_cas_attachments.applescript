-- FinResearch: an Apple Mail rule action that saves CAS PDF attachments into the statement inbox (issue #296).
--
-- Install it yourself (FinResearch never touches Mail): `uv run finresearch inbox setup-mail` prints the steps and,
-- with --script, this file with your inbox folder filled in. Mail runs rule scripts only from
-- ~/Library/Application Scripts/com.apple.mail/.
--
-- What it does, for each message the rule hands it:
--   * the sender's address must be at one of ALLOWED_DOMAINS (or a subdomain); anything else is ignored, so a broad
--     rule cannot feed other mail into the inbox. Mail does not verify senders: this is a filter, not authentication.
--   * only attachments named *.pdf or typed application/pdf; the saved file must start with "%PDF" or it is deleted.
--   * the file gets a neutral name (<domain>-<yyyymmdd-hhmmss>-<n>.pdf: a statement's own name can carry the PAN),
--     is chmod 600, and is moved into the inbox only once complete (it is written to the hidden .incoming folder
--     first, which the inbox ignores). An existing file is never overwritten.
-- Nothing is read from the PDF and no password is involved: the app opens the file with the passwords saved in
-- Profile -> Connections -> Statement inbox.

-- Sender domains of the CAS emails (community-sourced; confirm against the "From" of a statement you received):
--   camsonline.com    CAMS          donotreply@camsonline.com
--   kfintech.com      KFintech      samfS@kfintech.com / service@kfintech.com
--   nsdl.co.in        NSDL e-CAS    NSDL-CAS@nsdl.co.in   (nsdl.com: NSDL's other domain)
--   cdslstatement.com CDSL e-CAS    eCAS@cdslstatement.com (cdslindia.com: CDSL's main domain)
property ALLOWED_DOMAINS : {"camsonline.com", "kfintech.com", "nsdl.co.in", "nsdl.com", "cdslstatement.com", "cdslindia.com"}
property INBOX_PATH : "__FINRESEARCH_INBOX__"

using terms from application "Mail"
	on perform mail action with messages theMessages for rule theRule
		if INBOX_PATH starts with "__" then return -- not configured: print the script with `finresearch inbox setup-mail --script`
		set staging to INBOX_PATH & "/.incoming"
		do shell script "mkdir -p " & quoted form of staging & " && chmod 700 " & quoted form of INBOX_PATH & " " & quoted form of staging
		repeat with theMessage in theMessages
			try
				set theDomain to my senderDomain(theMessage)
				if my isAllowed(theDomain) then my savePdfs(theMessage, theDomain, staging)
			end try
		end repeat
	end perform mail action with messages
end using terms from

on senderDomain(theMessage)
	tell application "Mail" to set addr to extract address from (sender of theMessage)
	set AppleScript's text item delimiters to "@"
	set parts to text items of addr
	set AppleScript's text item delimiters to ""
	if (count of parts) is not 2 then return ""
	return my lower(item 2 of parts)
end senderDomain

on isAllowed(theDomain)
	if theDomain is "" then return false
	repeat with d in ALLOWED_DOMAINS
		if theDomain is (d as text) or theDomain ends with ("." & d) then return true
	end repeat
	return false
end isAllowed

on savePdfs(theMessage, theDomain, staging)
	set stamp to do shell script "date +%Y%m%d-%H%M%S"
	set n to 0
	tell application "Mail" to set atts to mail attachments of theMessage
	repeat with att in atts
		tell application "Mail"
			set attName to name of att
			set attType to ""
			try
				set attType to MIME type of att
			end try
		end tell
		if my lower(attName) ends with ".pdf" or attType is "application/pdf" then
			set n to n + 1
			set fileName to theDomain & "-" & stamp & "-" & n & ".pdf"
			set tmpPath to staging & "/" & fileName
			tell application "Mail" to save att in (POSIX file tmpPath)
			-- owner-only, a real PDF, then into the inbox without overwriting (mv -n); otherwise removed
			do shell script "f=" & quoted form of tmpPath & "; chmod 600 \"$f\" && [ \"$(head -c 4 \"$f\")\" = '%PDF' ] && mv -n \"$f\" " & quoted form of (INBOX_PATH & "/" & fileName) & "; rm -f \"$f\""
		end if
	end repeat
end savePdfs

on lower(t)
	return do shell script "printf %s " & quoted form of t & " | tr '[:upper:]' '[:lower:]'"
end lower
