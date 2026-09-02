const fs = require('fs');
const path = require('path');
const csv = require('csv-parser');

const inputFile = path.join(__dirname, 'input.csv'); // Your CSV file
const outputFile = path.join(__dirname, 'output.json'); // Output JSON file
const results = [];

fs.createReadStream(inputFile)
  .pipe(csv())
  .on('data', (row) => {
    if (row.iata_code && row.iata_code.trim() !== '') {
      results.push({
        id: row.id,
        name: row.name,
        iata_code: row.iata_code
      });
    }
  })
  .on('end', () => {
    fs.writeFileSync(outputFile, JSON.stringify(results, null, 2), 'utf8');
    console.log(`✅ JSON written to ${outputFile}`);
  });
