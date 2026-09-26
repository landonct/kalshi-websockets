#include "ixwebsocket/IXWebSocket.h"
#include "json.hpp"
#include <charconv>
#include <chrono>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <queue>
#include <stdexcept>
#include <string>
#include <thread>
using json = nlohmann::json;

enum class Side { Ask, Bid };
struct Trade {
  double price;
  int volume;
  Side taker_side; // 0 if no, 1 if yes

  Trade() = default;
  Trade(double price, int volume, Side taker_side)
      : price(price), volume(volume), taker_side(taker_side) {}

  friend std::ostream &operator<<(std::ostream &os, const Trade &trade) {
    std::string bid_ask = (trade.taker_side == Side::Bid) ? "bid" : "ask";
    os << trade.price << " cent " << bid_ask << ", " << trade.volume
       << " contracts" << "\n";

    return os;
  }
};

struct Row {
  std::string market_ticker = "";
  double yes_price;
  int volume;
  Side taker_side;
  std::time_t created_time;

  Row() = default;
  Row(const std::string &market_ticker, double yes_price, int volume,
      Side taker_side, std::time_t created_time)
      : market_ticker(market_ticker), yes_price(yes_price), volume(volume),
        taker_side(taker_side), created_time(created_time) {}

  Trade row2trade() const { return Trade(yes_price, volume, taker_side); }
};

class TradeQueue {
  std::queue<Trade> trade_queue;
  double total_price;
  int count;
  double average;
  const size_t max_size;

public:
  TradeQueue(int max_size)
      : total_price(0.0), count(0), average(0), max_size(max_size) {}
  TradeQueue() : TradeQueue(10) {}

  void push(const Trade &trade) {
    total_price += trade.price;
    trade_queue.push(trade);
    count++;
    if (trade_queue.size() > max_size) {
      Trade front_trade = trade_queue.front();
      total_price -= front_trade.price;
      trade_queue.pop();
      count--;
    }
    average = total_price / count;
  }

  double get_average() const { return average; }
};

void from_json(const json &j, Row &row,
               const std::string time_format = "%Y-%m-%dT%H:%M:%S.%fZ") {
  row.market_ticker = j.at("market_ticker").get<std::string>();
  row.yes_price = j.at("yes_price").get<double>();
  row.volume = j.at("count").get<int>();
  std::string taker = j.at("taker_side").get<std::string>();
  row.taker_side = (taker == "yes") ? Side::Ask : Side::Bid;
  std::string string_time = j.at("created_time").get<std::string>();
  std::tm t = {};
  std::sscanf(string_time.c_str(), "%d-%d-%dT%d:%d:%d", &t.tm_year, &t.tm_mon,
              &t.tm_mday, &t.tm_hour, &t.tm_min, &t.tm_sec);
  t.tm_year -= 1900;
  t.tm_mon -= 1;
  row.created_time = std::mktime(&t);
}

bool read_line(std::ifstream &input, json &outrow) {
  std::string line;

  if (getline(input, line)) {
    // Use a module to parse the json
    outrow = json::parse(line);
    return true;
  }

  return false;
}

int main(int argc, char *argv[]) {
  Trade trade = Trade(.99, 10, Side::Bid);
  std::cout << trade;

  json outrow;
  std::ifstream file("kalshi_mlb.json");

  if (!file.is_open()) {
    std::cout << "Error: File not found.\n";
  }

  TradeQueue rolling_window(10);
  ix::WebSocket webSocket;

  std::string url = "wss://api.kalshi.com/trade-api/ws/v2";
  webSocket.setUrl(url);

  webSocket.setOnMessageCallback([&](const ix::WebSocketMessagePtr &msg) {
    // 1. DIAGNOSTIC: Catch internal handshake or connection errors
    if (msg->type == ix::WebSocketMessageType::Error) {
      std::cerr << "❌ Network Error Encountered!\n";
      std::cerr << " Description: " << msg->errorInfo.reason << "\n";
      std::cerr << " HTTP Status: " << msg->errorInfo.http_status << "\n";
    }

    // 2. DIAGNOSTIC: Monitor close events
    if (msg->type == ix::WebSocketMessageType::Close) {
      std::cerr << "🔌 Connection Closed by Server. Reason: "
                << msg->closeInfo.reason << "\n";
    }

    if (msg->type == ix::WebSocketMessageType::Open) {
      std::cout << "Connected! Sending test data...\n";
      webSocket.send("{\"market_ticker\":\"MLB-2026\",\"yes_price\":0.55,"
                     "\"count\":15,\"taker_side\":\"yes\",\"created_time\":"
                     "\"2026-06-06T12:00:00.000Z\"}");
    }
    if (msg->type == ix::WebSocketMessageType::Message) {
      try {
        // msg->str contains the raw incoming text line from the server
        json outrow = json::parse(msg->str);

        // Convert the raw JSON into your native C++ types
        Row row = outrow;
        Trade trade = row.row2trade();

        // Push it into your high-performance calculator!
        rolling_window.push(trade);

        // Print the real-time updates!
        std::cout << "⚡ Live Trade Added: " << trade;
        std::cout << "📈 New 10-Trade Rolling Avg: "
                  << rolling_window.get_average() << "\n";
        std::cout << "--------------------------------------------------\n";
      } catch (const json::exception &e) {
        // If the server sends a heartbeat or a non-trade message, catch it
        // gracefully!
        std::cerr << "⚠️ Skipped non-trade payload: " << e.what() << "\n";
      } catch (const std::exception &e) {
        std::cerr << "❌ System Error: " << e.what() << "\n";
      }
    }
  });

  webSocket.start();

  while (true) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  return 0;
}