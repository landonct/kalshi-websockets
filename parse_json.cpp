#include <iostream>
#include <string>
#include <ctime>
#include <fstream>
#include <stdexcept>
#include <iomanip>
#include <charconv>
#include <queue>
#include "json.hpp"
using json = nlohmann::json;

enum class Side
{
    Ask,
    Bid
};
struct Trade
{
    double price;
    int volume;
    Side taker_side; // 0 if no, 1 if yes

    Trade() = default;
    Trade(double price, int volume, Side taker_side) : price(price), volume(volume), taker_side(taker_side) {}

    friend std::ostream &operator<<(std::ostream &os, const Trade &trade)
    {
        std::string bid_ask = (trade.taker_side == Side::Bid) ? "bid" : "ask";
        os << trade.price << " cent " << bid_ask << ", " << trade.volume << " contracts" << "\n";

        return os;
    }
};

struct Row
{
    std::string market_ticker = "";
    double yes_price;
    int volume;
    Side taker_side;
    std::time_t created_time;

    Row() = default;
    Row(const std::string &market_ticker, double yes_price, int volume, Side taker_side, std::time_t created_time)
        : market_ticker(market_ticker), yes_price(yes_price), volume(volume), taker_side(taker_side), created_time(created_time) {}

    Trade row2trade() const
    {
        return Trade(yes_price, volume, taker_side);
    }
};

class TradeQueue
{
    std::queue<Trade> trade_queue;
    double total_price;
    int count;
    double average;
    const size_t max_size;

public:
    TradeQueue(int max_size)
        : total_price(0.0), count(0), average(0), max_size(max_size) {}
    TradeQueue() : TradeQueue(10) {}

    void push(const Trade &trade)
    {
        total_price += trade.price;
        trade_queue.push(trade);
        count++;
        if (trade_queue.size() > max_size)
        {
            Trade front_trade = trade_queue.front();
            total_price -= front_trade.price;
            trade_queue.pop();
            count--;
        }
        average = total_price / count;
    }

    double get_average() const { return average; }
};

void from_json(const json &j, Row &row, std::string time_format = "%Y-%m-%dT%H:%M:%S.%fZ")
{
    row.market_ticker = j.at("market_ticker").get<std::string>();
    row.yes_price = j.at("yes_price").get<double>();
    row.volume = j.at("count").get<int>();
    std::string taker = j.at("taker_side").get<std::string>();
    row.taker_side = (taker == "yes") ? Side::Ask : Side::Bid;
    std::string string_time = j.at("created_time").get<std::string>();
    std::tm t = {};
    std::sscanf(string_time.c_str(), "%d-%d-%dT%d:%d:%d",
                &t.tm_year, &t.tm_mon, &t.tm_mday,
                &t.tm_hour, &t.tm_min, &t.tm_sec);
    t.tm_year -= 1900;
    t.tm_mon -= 1;
    row.created_time = std::mktime(&t);
}

bool read_line(std::ifstream &input, json &outrow)
{
    std::string line;

    if (getline(input, line))
    {
        // Use a module to parse the json
        outrow = json::parse(line);
        return true;
    }

    return false;
}

int main(int argc, char *argv[])
{
    Trade trade = Trade(.99, 10, Side::Bid);
    std::cout << trade;

    json outrow;
    std::ifstream file("kalshi_mlb.json");

    if (!file.is_open())
    {
        std::cerr << "Error: File not found.\n";
        return 1;
    }

    TradeQueue rolling_window(10);

    while (read_line(file, outrow))
    {
        Row row;
        Trade trade;
        try
        {
            row = outrow;
            trade = row.row2trade();

            rolling_window.push(trade);

            std::cout << "Processed trade: " << trade;
            std::cout << "Rolling Average Price: " << rolling_window.get_average() << "\n\n";
        }
        catch (const json::exception &e)
        {
            std::cerr << "Error parsing the JSON " << e.what() << "\n";
        }
        catch (const std::exception &e)
        {
            std::cerr << "An error occured: " << e.what() << "\n";
        }
    }

    return 0;
}