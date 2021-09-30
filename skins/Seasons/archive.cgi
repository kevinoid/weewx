#!/bin/sh
# archive.cgi - Export weeWX data as a CSV
# shellcheck shell=dash

set -Ceu

WEEWX_DB=/var/lib/weewx/weewx.sdb

case "${REQUEST_METHOD-}" in
GET) ;;
*)
	echo "Content-Type: text/plain
Status: 405 Method Not Allowed
Allow: GET

Error: ${REQUEST_METHOD-} is not supported.  Only HTTP GET is supported."
	exit
	;;
esac

# TODO: Send status 406 if text/csv is not acceptable (based on $HTTP_ACCEPT).

date_to_unix() {
	case "$1" in
	'') return 1 ;;
	*[!0-9]*) LC_ALL=C date -d "$1" +%s ;;
	*) echo "$1" ;;
	esac
}

get_query_date() {
	local datestr
	case "${QUERY_STRING-}" in
	"$1="* | *"&$1="*)
		datestr=${QUERY_STRING#"$1="}
		datestr=${datestr##*"&$1="}
		datestr=${datestr%%'&'*}
		if [ -n "$datestr" ] && ! date_to_unix "$datestr" 2>/dev/null; then
			echo "Content-Type: text/plain
Status: 400 Bad Request

Error: Invalid date '$datestr' for $1." >&3
			return 1
		fi
		;;
	esac
}

begin=$(get_query_date begin) 3>&1 || exit
end=$(get_query_date end) 3>&1 || exit

# Create filename from begin/end
begin_date=
if [ -n "$begin" ]; then
	begin_date=$(date -d "@$begin" +%F)
fi
if [ -n "$end" ]; then
	end_date=$(date -d "@$((end - 1))" +%F)
else
	end_date=$(date +%F)
fi
if [ -z "$begin_date" ] || [ "$begin_date" = "$end_date" ]; then
	begin_end=$end_date
else
	begin_end=$begin_date-to-$end_date
fi
filename=weather-$begin_end.csv

echo "Content-Type: text/csv; charset=utf-8; header=present
Content-Disposition: attachment; filename=\"$filename\""

# Blank line to separate response headers from body
echo

if [ -n "$begin" ] && [ -n "$end" ]; then
	where="WHERE dateTime >= $begin AND dateTime < $end"
elif [ -n "$begin" ]; then
	where="WHERE dateTime >= $begin"
elif [ -n "$end" ]; then
	where="WHERE dateTime < $end"
else
	where=
fi

exec sqlite3 -csv -header "file://$WEEWX_DB?mode=ro" <<SQL
SELECT datetime(dateTime, 'unixepoch', 'localtime') AS dateTimeISO,
    *
FROM archive
$where
ORDER BY dateTime ASC
SQL
